#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client_zipxml.py — 复刻 SinoCC 客户端真实协议（P0 抓包解密版）

协议（2026-08-17 抓包实证，非 SOAP envelope！）:
  请求: POST /jsp/{wfman|bomman}.jsp?loginguid=<token>&classid=<类>&action=<动作>
        Content-Type: image/jpeg
        cpccookie: sessiontimestamp=<毫秒>
        User-Agent: Mozilla/3.0 (compatible;Super Indy Library)
        body = 0x02 + ZIP{ "0": UTF-16 LE XML "<Request><Data>…</Data></Request>" }
  响应: Content-Type: text/xml; charset=utf-8, Content-Encoding: zip
        body = 0x03 0x02 + ZIP{ "get.xml": XML }

登录复用 web JSON 通道（loginman.jsp loginuser.login，已验证可用）。
"""
import asyncio
import base64
import hashlib
import io
import logging
import re
import time
import struct
import zipfile
import zlib
import uuid
import xml.etree.ElementTree as ET

import httpx

from app.config import Settings
from app.client import SinoCCClient
from app.bom_templates import BOM_SELECTREF, ITEM_GETREFERENCE
from app.wf_templates import (
    WF_SUBMIT, WF_REJECT, WF_SELECT, WF_GETSUBMITNODEINFO,
)
from app.doc_templates import build_doc_row, build_fdr_row

log = logging.getLogger("sinocc.zipclient")

# 真实客户端机器名（抓包实证 2026-08-18；登录请求必填字段之一）
CLIENT_NAME = "JF6R7G3:NT"

# 物料确认单(SamConfirm)详情请求模板（2026-08-20 抓包实证：ecmman.jsp?classid=cpcecnsamconfirm&action=select）
SAMCONFIRM_SELECT_TEMPLATE = (
    '<samcon_id>64149</samcon_id><samcon_code></samcon_code><formtype>0</formtype><proj_no></proj_no><sta'
    't_rev></stat_rev><item_type></item_type><item_import>0</item_import><std_type>0</std_type><invorg_id'
    '>0</invorg_id><is_exp_to_erp>0</is_exp_to_erp><vendor></vendor><vendorname></vendorname><vendor_id><'
    '/vendor_id><vendor_code></vendor_code><item_small_code></item_small_code><bat_manu></bat_manu><if_af'
    'fect_cost>0</if_affect_cost><delivery_sam_code></delivery_sam_code><ref_con_code></ref_con_code><ori'
    'g_item_code></orig_item_code><new_item_id>0</new_item_id><new_item_code></new_item_code><con_note></'
    'con_note><con_fund></con_fund><per_and_struct></per_and_struct><usage_per></usage_per><note></note><'
    'con_conclusion></con_conclusion><send_dep></send_dep><send_depid></send_depid><send_vendor></send_ve'
    'ndor><wfid>0</wfid><wfflag>0</wfflag><stat>0</stat><wfright>0</wfright><procid>0</procid><wftempid>0'
    '</wftempid><creator></creator><create_time></create_time><updator></updator><update_time></update_ti'
    'me><spec></spec><material></material><weight>0</weight><draw_id></draw_id><sam_name></sam_name><appl'
    'y_id>0</apply_id><order_id>0</order_id><cust_draw_id></cust_draw_id><purchmrporgid></purchmrporgid><'
    'purchmrporgname></purchmrporgname><samcon_type></samcon_type><con_type>0</con_type><sqlwhere></sqlwh'
    'ere><maxsearchrltcmt>0</maxsearchrltcmt><fin_sam_time></fin_sam_time><con_fin_time></con_fin_time><s'
    'am_reason></sam_reason><sam_qty></sam_qty><invorgname></invorgname><ecncode></ecncode><brand></brand'
    '><sourcemanuf></sourcemanuf><cust_model></cust_model><agent></agent><appointreason></appointreason><'
    'appointbrand></appointbrand><iscert>0</iscert><samanddiff></samanddiff><poisonreport></poisonreport>'
    '<relatedcode></relatedcode><relatingcode></relatingcode><drawdocname></drawdocname><procuser1></proc'
    'user1><procuser2></procuser2><procuser3></procuser3><procuser4></procuser4><procuser5></procuser5><p'
    'rocuser6></procuser6><procuser7></procuser7><procuser8></procuser8><procuser9></procuser9><procuser1'
    '0></procuser10><procuser11></procuser11><procuser12></procuser12><procuser13></procuser13><procuser1'
    '4></procuser14><procuser15></procuser15><procuser16></procuser16><procuser17></procuser17><procuser1'
    '8></procuser18><procuser19></procuser19><procuser20></procuser20><signtime1></signtime1><signtime2><'
    '/signtime2><signtime3></signtime3><signtime4></signtime4><signtime5></signtime5><signtime6></signtim'
    'e6><signtime7></signtime7><signtime8></signtime8><signtime9></signtime9><signtime10></signtime10><si'
    'gntime11></signtime11><signtime12></signtime12><signtime13></signtime13><signtime14></signtime14><si'
    'gntime15></signtime15><signtime16></signtime16><signtime17></signtime17><signtime18></signtime18><si'
    'gntime19></signtime19><signtime20></signtime20><vendorkeyitemlist></vendorkeyitemlist><defmemo1></de'
    'fmemo1><defmemo2></defmemo2><defmemo3></defmemo3><defmemo4></defmemo4><def1></def1><def2></def2><def'
    '3></def3><def4></def4><def5></def5><def6></def6><searchflag>0</searchflag><certrelate>0</certrelate>'
    '<svendorid>0</svendorid><sapplyid>0</sapplyid><supinvorgidnames></supinvorgidnames><statime></statim'
    'e><endtime></endtime><orgid>0</orgid><orgname></orgname><svendororgid>0</svendororgid><manufacturety'
    'pe>0</manufacturetype><universaltype>0</universaltype><isrohs>0</isrohs><erpvendorid>0</erpvendorid>'
    '<vendorsiteid>0</vendorsiteid><vendorsrmstatus></vendorsrmstatus><entid>0</entid><need3c>0</need3c><'
    'is3c>0</is3c><moldcompactcode></moldcompactcode><expiretimes></expiretimes><confirmstatus>0</confirm'
    'status><expirereason></expirereason><vendor_status_code></vendor_status_code><vendor_status_meaning>'
    '</vendor_status_meaning><qty>0</qty><category_name></category_name><new_category_name></new_category'
    '_name><samconrepealflag>0</samconrepealflag><itemname></itemname><pctid>0</pctid><objattachs></objat'
    'tachs><ecnsamconfirmlineofecnsamconfirms></ecnsamconfirmlineofecnsamconfirms><cpcecnsamconfirmitemso'
    'fecnsamconfirms></cpcecnsamconfirmitemsofecnsamconfirms>'
)

# 产品投产通知单(PDTBatchApply)详情请求模板（2026-08-31 抓包实证 prodlaunch_popup_v1：
# ecmman.jsp?classid=cpcpdtbatchapply&action=select，请求体 465 字节，仅 <applyid> 有效其余留空）
PDTBATCHAPPLY_SELECT_TEMPLATE = (
    '<applyid>582</applyid><applycode></applycode><invorgid>0</invorgid><code></code>'
    '<projid>0</projid><projname></projname><manager></manager><projphasename></projphasename>'
    '<projphaseid>0</projphaseid><creator></creator><createtime></createtime><updator></updator>'
    '<updatetime></updatetime><stat>0</stat><wfflag>0</wfflag><wfid>0</wfid><procid>0</procid>'
    '<wfright>0</wfright><wftempid>0</wftempid><orgid>0</orgid><orgname></orgname><note></note>'
    '<sqlwhere></sqlwhere><maxsearchrltcmt>0</maxsearchrltcmt><objattachs></objattachs>'
    '<pdtbatchapplylineofpdtbatchapplys></pdtbatchapplylineofpdtbatchapplys>'
)

# 更改通知单(ECN)详情请求模板（2026-08-20 抓包实证：ecmman.jsp?classid=cpcecn&action=select）
ECN_SELECT_TEMPLATE = (
    '<ecnid>91362</ecnid><ecnname></ecnname><ecntype>0</ecntype><ecrcode></ecrcode><ntcode></ntcode><proj'
    'code></projcode><invorgid>0</invorgid><invorgname></invorgname><pdtcode></pdtcode><pdtdrawid></pdtdr'
    'awid><pdtname></pdtname><mtlinvqty></mtlinvqty><mtlpurchqty></mtlpurchqty><cdata></cdata><costinfect'
    '></costinfect><pdtinfect></pdtinfect><mtldealbc></mtldealbc><mtldealac></mtldealac><creasontype></cr'
    'easontype><pretreat></pretreat><consigner></consigner><subscriptioncode></subscriptioncode><pmcode><'
    '/pmcode><checkdetail></checkdetail><schedulingcheck></schedulingcheck><customconfirm></customconfirm'
    '><phase></phase><iscert></iscert><iscustom></iscustom><rebom></rebom><changecheck></changecheck><pha'
    'sepmcode></phasepmcode><implementdate></implementdate><releasedate></releasedate><creason></creason>'
    '<mtlimp></mtlimp><mtltype></mtltype><itemstdtype></itemstdtype><inerp>0</inerp><sampletecver></sampl'
    'etecver><purchmrporg></purchmrporg><purchmrporgname></purchmrporgname><cmitemcode></cmitemcode><cmit'
    'emdrawid></cmitemdrawid><cmitemname></cmitemname><cmitempurchflag></cmitempurchflag><mtlmincode></mt'
    'lmincode><cmitemmtl></cmitemmtl><cmitemweight></cmitemweight><samplecode></samplecode><supplymbill><'
    '/supplymbill><supplyname></supplyname><refcmcode></refcmcode><batchabt></batchabt><cmgist></cmgist><'
    'mfun></mfun><mfunattach>0</mfunattach><mfunattachname></mfunattachname><cmcontent></cmcontent><remar'
    'k></remark><cmconclusion></cmconclusion><olddrawdescription></olddrawdescription><newdrawdescription'
    '></newdrawdescription><drawchart>0</drawchart><drawchartname></drawchartname><sendtoorg></sendtoorg>'
    '<sendtosupply></sendtosupply><batchcnote></batchcnote><invmtldealnote></invmtldealnote><cpcorgname><'
    '/cpcorgname><cpcuser></cpcuser><startday></startday><endday></endday><searchflag>0</searchflag><orgn'
    'ame></orgname><idpath></idpath><mrporgname></mrporgname><typecount>0</typecount><rate>0</rate><itemc'
    'ode></itemcode><itemname></itemname><supercode></supercode><drawid></drawid><istempcode>False</istem'
    'pcode><itemid>0</itemid><sflag>0</sflag><itemdesc></itemdesc><ecncheck>0</ecncheck><checkbom>0</chec'
    'kbom><checkitem>0</checkitem><checkdraw>0</checkdraw><notnewcheck>0</notnewcheck><notnewcheckbom>0</'
    'notnewcheckbom><notnewcheckitem>0</notnewcheckitem><notnewcheckdraw>0</notnewcheckdraw><batchitemcod'
    'e></batchitemcode><batchopernote></batchopernote><superdesc></superdesc><note></note><creator></crea'
    'tor><createtime></createtime><updator></updator><updatetime></updatetime><sqlwhere></sqlwhere><maxse'
    'archrltcmt>0</maxsearchrltcmt><stat>0</stat><wfid>0</wfid><procid>0</procid><wfright>0</wfright><wff'
    'lag>0</wfflag><wftempid>0</wftempid><ecntypename></ecntypename><batchfrom></batchfrom><batchto></bat'
    'chto><orgid>0</orgid><mainecnid>0</mainecnid><def1></def1><def2></def2><def3></def3><def4></def4><de'
    'f5></def5><def6></def6><def7></def7><def8></def8><def9></def9><def10></def10><def11></def11><def12><'
    '/def12><def13></def13><def14></def14><def15></def15><def16></def16><def17></def17><def18></def18><de'
    'f19></def19><def20></def20><def21></def21><def22></def22><def23></def23><def24></def24><def25></def2'
    '5><def26></def26><def27></def27><def28></def28><def29></def29><def30></def30><defmemo1></defmemo1><d'
    'efmemo2></defmemo2><defmemo3></defmemo3><defmemo4></defmemo4><defmemo5></defmemo5><defcaption1></def'
    'caption1><defcaption2></defcaption2><defcaption3></defcaption3><defcaption4></defcaption4><defcaptio'
    'n5></defcaption5><defcaption6></defcaption6><defcaption7></defcaption7><defcaption8></defcaption8><d'
    'efcaption9></defcaption9><defcaption10></defcaption10><defcaption11></defcaption11><defcaption12></d'
    'efcaption12><defcaption13></defcaption13><defcaption14></defcaption14><defcaption15></defcaption15><'
    'defcaption16></defcaption16><defcaption17></defcaption17><defcaption18></defcaption18><defcaption19>'
    '</defcaption19><defcaption20></defcaption20><defcaption21></defcaption21><defcaption22></defcaption2'
    '2><defcaption23></defcaption23><defcaption24></defcaption24><defcaption25></defcaption25><defcaption'
    '26></defcaption26><defcaption27></defcaption27><defcaption28></defcaption28><defcaption29></defcapti'
    'on29><defcaption30></defcaption30><procuser1></procuser1><procuser2></procuser2><procuser3></procuse'
    'r3><procuser4></procuser4><procuser5></procuser5><procuser6></procuser6><procuser7></procuser7><proc'
    'user8></procuser8><procuser9></procuser9><procuser10></procuser10><procuser11></procuser11><procuser'
    '12></procuser12><procuser13></procuser13><procuser14></procuser14><procuser15></procuser15><procuser'
    '16></procuser16><procuser17></procuser17><procuser18></procuser18><procuser19></procuser19><procuser'
    '20></procuser20><signtime1></signtime1><signtime2></signtime2><signtime3></signtime3><signtime4></si'
    'gntime4><signtime5></signtime5><signtime6></signtime6><signtime7></signtime7><signtime8></signtime8>'
    '<signtime9></signtime9><signtime10></signtime10><signtime11></signtime11><signtime12></signtime12><s'
    'igntime13></signtime13><signtime14></signtime14><signtime15></signtime15><signtime16></signtime16><s'
    'igntime17></signtime17><signtime18></signtime18><signtime19></signtime19><signtime20></signtime20><o'
    'pinion1></opinion1><opinion2></opinion2><opinion3></opinion3><opinion4></opinion4><opinion5></opinio'
    'n5><opinion6></opinion6><opinion7></opinion7><opinion8></opinion8><opinion9></opinion9><opinion10></'
    'opinion10><opinion11></opinion11><opinion12></opinion12><opinion13></opinion13><opinion14></opinion1'
    '4><opinion15></opinion15><opinion16></opinion16><opinion17></opinion17><opinion18></opinion18><opini'
    'on19></opinion19><opinion20></opinion20><bdomanualweave>0</bdomanualweave><wffromstatime></wffromsta'
    'time><wftostatime></wftostatime><wffromendtime></wffromendtime><wftoendtime></wftoendtime><endtime><'
    '/endtime><ecrstat>0</ecrstat><auto_effect>0</auto_effect><betweenitemcode></betweenitemcode><enablet'
    'ype>0</enabletype><checkmsg></checkmsg><suppubsavetype>0</suppubsavetype><keycid>0</keycid><lockstat'
    '>0</lockstat><lockuserid>0</lockuserid><lockusername></lockusername><locktime></locktime><costanalyz'
    'e></costanalyze><costanalyzeext></costanalyzeext><applydept></applydept><wftype>0</wftype><isissue>0'
    '</isissue><costdept></costdept><costdeptid>0</costdeptid><ismaintain>0</ismaintain><samcon_id>0</sam'
    'con_id><abandonhints></abandonhints><cpcecnitemlineofcpcecns></cpcecnitemlineofcpcecns><cpcecnbomlin'
    'eofcpcecns></cpcecnbomlineofcpcecns><cpcecndrawlineofcpcecns></cpcecndrawlineofcpcecns><cpcecntransf'
    'ormlineofcpcecns></cpcecntransformlineofcpcecns><cpcecnbatchclineofcpcecns></cpcecnbatchclineofcpcec'
    'ns><cpcecnmtldeallineofcpcecns></cpcecnmtldeallineofcpcecns><cpcecnbomsulineofcpcecns></cpcecnbomsul'
    'ineofcpcecns><objattachs></objattachs><cpcecns></cpcecns><cpcecndocofcpcecns></cpcecndocofcpcecns><c'
    'pcecnrelationofcpcecns></cpcecnrelationofcpcecns><cpcecnpdlineofcpcecns></cpcecnpdlineofcpcecns><cpc'
    'ecnbomdaltlineofcpcecns></cpcecnbomdaltlineofcpcecns><cpcecnbomheadofcpcecns></cpcecnbomheadofcpcecn'
    's><cpcecnbomdexcplineofcpcecns></cpcecnbomdexcplineofcpcecns><ecnsuppublineofecns></ecnsuppublineofe'
    'cns><shtattachs></shtattachs><ecnreldrawlineofecns></ecnreldrawlineofecns><ecnbomtosubecnofecns></ec'
    'nbomtosubecnofecns><ecnrefprodlineofecns></ecnrefprodlineofecns><ecnitemmcpropofecns></ecnitemmcprop'
    'ofecns><cpcecnsamconfirminfoofcpcecns></cpcecnsamconfirminfoofcpcecns>'
)


def _fill_template(template: str, fields: dict) -> str:
    """把模板中 <field>xxx</field> 替换为指定值（保留其余原值）。"""
    s = template
    for field, val in fields.items():
        s = re.sub(rf"<{field}>[^<]*</{field}>", f"<{field}>{val}</{field}>", s, count=1)
    return s


def _extract_data_inner(xml: str) -> str:
    """从 <response><data>...</data></response> 或 <Request><Data>...</Data></Request>
    中提取内部字段内容（含子标签）。"""
    m = re.search(r"<data[^>]*>(.*)</data>\s*</response>\s*$", xml, re.S | re.I)
    if m:
        return m.group(1)
    m = re.search(r"<Data[^>]*>(.*)</Data>\s*</Request>\s*$", xml, re.S | re.I)
    if m:
        return m.group(1)
    # 兜底：直接返回原字符串（已经是内部字段）
    return xml


def xml_fields(xml: str) -> dict:
    """取响应 data 层的**头部字段**（已剔除子表块，避免 dict() 取到子表内同名值）。

    「主表 + 子表」响应用 dict(findall) 会把子表最后一个同名值当成头部值
    （如 <docname> 出现在 docofsubdraws 子行里）。这里先把所有 ``<tag>`` 包裹的
    子表块整体剔掉，再取字段。
    """
    inner = _extract_data_inner(xml)
    if not inner:
        return {}
    # 逐轮剥离"含嵌套标签的块"（子表），叶子字段 <tag>值</tag> 保留
    stripped = inner
    pat = re.compile(r"<([a-zA-Z_0-9]+)>([\s\S]*?)</\1>")
    for _ in range(6):
        stripped, n = pat.subn(lambda m: "" if "<" in m.group(2) else m.group(0), stripped)
        if not n:
            break
    return dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", stripped))


# 兼容旧命名
_xml_fields = xml_fields


def parse_bom_response(xml_text: str):
    """解析客户端通道 BOM 响应 XML → (根信息 dict, 子件 dict 列表)。"""
    from lxml import etree
    root = etree.fromstring(xml_text.encode("utf-8", errors="ignore"))
    data = root.find("data")
    if data is None:
        return {}, []
    root_info = {etree.QName(c).localname: (c.text or "") for c in data
                 if etree.QName(c).localname != "bomofboms"}
    children = []
    bb = data.find("bomofboms")
    if bb is not None:
        for el in bb.iterfind("bomofbom"):
            children.append({etree.QName(c).localname: (c.text or "") for c in el})
    return root_info, children


def build_body(data_xml: str) -> bytes:
    """data_xml: <Request><Data>...</Data></Request> 内部字段；返回 0x02+ZIP。"""
    xml = f'<?xml version="1.0" encoding="UTF-16"?><Request><Data>{data_xml}</Data></Request>'
    raw = xml.encode("utf-16")  # 带 BOM 的 UTF-16 LE
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("0", raw)
    return b"\x02" + bio.getvalue()


def parse_response(body: bytes) -> str:
    """解析 0x03/0x02 + ZIP{get.xml} → 返回 XML 文本。"""
    raw = body
    while raw[:1] in (b"\x02", b"\x03"):
        raw = raw[1:]
    if raw[:4] != b"PK\x03\x04":
        return raw.decode("utf-8", "replace")
    z = zipfile.ZipFile(io.BytesIO(raw))
    content = z.read("get.xml")
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return content.decode("utf-16")
    return content.decode("utf-8", "replace")


class SinoCCZipClient:
    def __init__(self, settings: Settings, account: str = None, password: str = None):
        self.settings = settings
        self.account = account
        self.password = password
        self.login_uid = None
        self._login_timestamp = None
        self.username = None  # 服务端权威真实姓名（登录响应 <username> 或 whoami 反查）
        # 2026-09-02：session store 里的当前登录用户（由 router 注入）。
        # session-only 注入（/auth/auto、/auth/session）时 self.account 恒为 None，
        # 此前各处用 `self.account or "唐阳"` 兜底 —— 会把假用户名写进请求 XML。
        self.user = None
        self.session = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            trust_env=self.settings.trust_env,     # 2026-09-07：与老通道一致的网络出口配置
            proxy=self.settings.proxy or None,
        )
        self._web = None

    def set_login_uid(self, login_uid: str) -> "SinoCCZipClient":
        """注入已有 loginUID（session store 模式，免账号密码）。"""
        self.login_uid = login_uid
        return self

    def set_user(self, user: str) -> "SinoCCZipClient":
        """注入当前登录用户名（session store 的 sess.user）。

        过滤空值与占位符 "session-only"，避免把占位串写进协议 XML。
        """
        if user and user != "session-only":
            self.user = user
            # 未显式传 account 时，用户名即账号（PDM 账号就是中文实名）
            if not self.account:
                self.account = user
        return self

    def _me(self) -> str:
        """当前操作者账号：显式 account > 注入的当前用户 > 登录响应真名。

        取不到时返回空串（**不再硬编码「唐阳」**）—— 宁可让服务端报缺字段，
        也不要静默用别人的身份提交流程。
        """
        return self.account or self.user or self.username or ""

    async def ensure_login(self) -> str:
        if self.login_uid:
            return self.login_uid
        # 优先走「客户端 ZIP 通道登录」（2026-08-18 抓包实证）：
        #   1) 客户端自生成 GUID，作为 loginman.jsp?loginguid={GUID}
        #   2) Content-Type: image/jpeg，body = 0x02+ZIP{"0": UTF-16 XML}
        #   3) XML 含 userid=账号~0 / userpass=base64(密码) / clientversion=3,7,1,0 / loginguid={GUID}
        #   4) 响应 Content-Encoding: zip → get.xml → <loginguid>CHIGOPDM.a.{GUID}.e69e...uacpc</loginguid>
        # 此方式拿到与真实客户端同构的「干净 GUID + uacpc」会话（web JSON 登录拿不到）。
        try:
            lg, ts = await self.client_login()
            self.login_uid = lg
            self._login_timestamp = ts
            return lg
        except Exception as e:
            log.warning("client_login 失败，回退 web JSON 登录: %s", e)
            web = SinoCCClient(self.settings, account=self.account, password=self.password)
            sess = await web.login()
            self.login_uid = sess.login_uid
            self._web = web
            return self.login_uid

    async def client_login(self) -> tuple:
        """客户端 ZIP 通道登录（真实 SinoCC.exe 同款），返回 (loginguid, sessiontimestamp)。"""
        # 2026-09-02：去掉「唐阳」硬编码兜底，改取当前真实账号
        account = self._me()
        password = self.password or ""
        if not password:
            raise RuntimeError("client_login requires password")
        if not account:
            raise RuntimeError("client_login requires account（未登录且无法自动探测当前用户）")
        guid = "{" + str(uuid.uuid4()).upper() + "}"
        # 注意：只传 <Data> 内部字段，build_body 会统一包成 <Request><Data>...</Data></Request>
        xml = (
            f"<userid>{account}~0</userid><sysuserid>0</sysuserid><username></username><clas>0</clas>"
            f"<userpass>{base64.b64encode(password.encode()).decode()}</userpass>"
            f"<loginguid>{guid}</loginguid><clientversion>3,7,1,0</clientversion><refuse>0</refuse>"
            f"<md5sn>{hashlib.md5(password.encode()).hexdigest()}</md5sn>"
            f"<entcode>{self.settings.default_entcode}</entcode><vaultid>1</vaultid><langid>1</langid>"
            f"<clientname>{CLIENT_NAME}</clientname>"
            f"<authsn></authsn><needauth>0</needauth><ismainsob>2</ismainsob>"
        )
        body = build_body(xml)
        r = await self.session.post(
            f"{self.settings.base_url}/jsp/loginman.jsp",
            params={"loginguid": guid, "classid": "loginuser", "action": "login"},
            content=body,
            headers={
                "Content-Type": "image/jpeg",
                "User-Agent": "Mozilla/3.0 (compatible;Super Indy Library)",
                "Accept": "text/html, */*",
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(f"client_login failed: {r.status_code} {r.content[:200]!r}")
        ts = ""
        m = re.search(r"sessiontimestamp=(\d+)", r.headers.get("cpccookie", ""))
        if m:
            ts = m.group(1)
        data = r.content
        while data[:1] in (b"\x02", b"\x03"):
            data = data[1:]
        if data[:4] != b"PK\x03\x04":
            raise RuntimeError(f"client_login bad response: {data[:120]!r}")
        z = zipfile.ZipFile(io.BytesIO(data))
        txt = z.read("get.xml").decode("utf-8", "replace")
        lg = re.search(r"<loginguid>([^<]*)</loginguid>", txt)
        if not lg:
            raise RuntimeError(f"client_login no loginguid in response: {txt[:200]}")
        # 登录响应 get.xml 直接返回服务端权威用户名（2026-08-26 实证 myfiles_login_capture）：
        #   <userid>唐阳</userid><username>唐阳</username>（+ sysuserid/comporgname/deptorgname 等）
        un = re.search(r"<username>([^<]*)</username>", txt)
        self.username = (un.group(1).strip() if un else "") or self.account or ""
        self._login_timestamp = ts
        log.info("client_login OK user=%s loginUID=%s... ts=%s", self.username or account, lg.group(1)[:40], ts)
        return lg.group(1), ts

    async def whoami(self, hint_userid: str = "") -> str:
        """用当前 loginUID 反查服务端权威用户名（免账号密码的 loginUID 注入场景）。

        2026-08-26 实证（myfiles_login_capture）：
        - 登录响应 get.xml 已含 <username>唐阳</username>，账号密码登录时直接可用。
        - 但 loginUID 注入（/session、/auto）没有密码，无法重放登录。
        - 服务端所有「查自己」接口（cpcworkspace.selectall / loginuser.getsyscache）都
          **回显请求体 userid 而不校验归属**，userid 留空一律返回空 → 无法靠 loginUID 反推用户名。
        故 whoami 依赖 hint_userid（前端 localStorage 记住的账号）来验证并取回真名。
        返回空串表示无法确定（调用方回退 account / "session-only"）。
        """
        if self.username:
            return self.username
        if not self.login_uid:
            try:
                await self.ensure_login()
            except Exception:
                return ""
        try:
            xml = await self.workspace_selectall(wstype="4", modid="12", userid=hint_userid)
            m = re.search(r"<wsname>([^<]*?)的工作区</wsname>", xml)
            if m and m.group(1).strip():
                self.username = m.group(1).strip()
                return self.username
            # 兜底：响应里 <userid> 回显（去掉 ~0 后缀）
            m2 = re.search(r"<userid>([^<]*)</userid>", xml)
            if m2:
                v = m2.group(1).strip().rstrip("~0").strip()
                if v and "~" not in v:
                    self.username = v
                    return v
        except Exception as e:
            log.warning("whoami 反查失败: %s", e)
        return ""

    async def call(self, jsp: str, classid: str, action: str, data_xml: str) -> str:
        """发送客户端协议请求，返回响应 XML 文本。"""
        if not self.login_uid:
            await self.ensure_login()
        body = build_body(data_xml)
        url = f"{self.settings.base_url}/jsp/{jsp}?loginguid={self.login_uid}&classid={classid}&action={action}"
        headers = {
            "Content-Type": "image/jpeg",
            "User-Agent": "Mozilla/3.0 (compatible;Super Indy Library)",
            "cpccookie": f"sessiontimestamp={int(time.time() * 1000)}",
        }
        resp = await self.session.post(url, content=body, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return parse_response(resp.content)

    # ---- 工作流 ----
    async def wf_list(self, wftempid: str = "0", flag: str = "1") -> str:
        return await self.call("wfman.jsp", "cpcwf", "selectall",
                               f"<wfid>0</wfid><wftempid>{wftempid}</wftempid><flag>{flag}</flag>")

    async def wf_selectref(self, wftempid: str = "475", flag: str = "1") -> str:
        return await self.call("wfman.jsp", "cpcwf", "selectref",
                               f"<wfid>0</wfid><wftempid>{wftempid}</wftempid><flag>{flag}</flag>")

    async def wf_detail(self, wfid: str) -> str:
        data = _fill_template(WF_SELECT, {"wfid": wfid})
        return await self.call("wfman.jsp", "cpcwf", "select", data)

    # ---- 发起流程（2026-09-02 两轮抓包：file_wf_start_v1 / file_wf_create_v1） ----
    # 链路：右键「工作流」→ 选流程（常用 workman cpcuserwftemp.search |
    #        搜索 wfman cpcwftemp.search）→ 编制界面（wfman cpcwftemp.select，
    #        wfobjs=objid_objtype）→ 提交 = cpcwf.insert → cpcwf.select → cpcwfproc.submit

    async def wf_user_wftemps(self) -> str:
        """常用流程列表（workman cpcuserwftemp search，抓包实证）：
        请求体 userid 空 + ismanual=0 → 服务端按会话返回全部可用流程；
        响应子表 <userwftemps><UserWFTemp>（wftempid/wftempname/namepath/ismanual…）。"""
        data = ("<userid></userid><wftempid>0</wftempid><wftempname></wftempname>"
                "<namepath></namepath><ismanual>0</ismanual><userwftemps></userwftemps>")
        return await self.call("workman.jsp", "cpcuserwftemp", "search", data)

    async def wf_wftemp_search(self, keyword: str) -> str:
        """流程模板关键字搜索（wfman cpcwftemp search，抓包实证）：
        关键字走 <sqlwhere> lower(WFTempName) like '%kw%'</sqlwhere>（前导空格 + lower() + %…%）。
        响应子表 <wftempofwftemps><wftempofwftemp>（模板全字段）。"""
        kw = keyword.strip().replace("&", "&amp;").replace("<", "&lt;").replace("'", "&apos;")
        sql = f" lower(WFTempName) like '%{kw}%'"
        data = ("<wftempid>0</wftempid><wftempname></wftempname><rev>0</rev><code></code><codeid>0</codeid>"
                "<clas>0</clas><stat>0</stat><managers></managers><actived>0</actived><submitctrl>0</submitctrl>"
                "<period>0</period><breakprocid>0</breakprocid><keephistory>0</keephistory><canselect>0</canselect>"
                "<creator></creator><createtime></createtime><updator></updator><updatetime></updatetime>"
                "<updatereason></updatereason><publishor></publishor><publishtime></publishtime>"
                "<publishstage>0</publishstage><publishstatus>0</publishstatus><issecret>0</issecret><note></note>"
                "<comporgid>0</comporgid><comporgname></comporgname><breakfunc></breakfunc><modifyperiod>0</modifyperiod>"
                "<schedulerid>0</schedulerid><schedulerdesc></schedulerdesc><objaccess>0</objaccess><isupdateright>0</isupdateright>"
                "<parentid></parentid><parenttype></parenttype><objid>0</objid><objtype>0</objtype><namepath></namepath>"
                "<codename></codename><maxsearchrltcmt>300</maxsearchrltcmt>"
                f"<sqlwhere>{sql}</sqlwhere>"
                "<sysbuiltinobj>0</sysbuiltinobj><canmodifytime>0</canmodifytime><idpath></idpath><typepath></typepath>"
                "<showtypeflag>0</showtypeflag><wfobjs></wfobjs><autosubmitmode>0</autosubmitmode>"
                "<comisnotemailto>0</comisnotemailto><wfnewmark>0</wfnewmark><autosubmitfirst>0</autosubmitfirst>"
                "<wftype>0</wftype><openmode>0</openmode><oneobjshow>0</oneobjshow><detaildispmode>0</detaildispmode>"
                "<prerun>0</prerun><objdefprops></objdefprops><objcrosss></objcrosss><wfproctempofwftemps></wfproctempofwftemps>"
                "<wfobjtempofwftemps></wfobjtempofwftemps><wftempofwftemps></wftempofwftemps>"
                "<proccondtempofwftemps></proccondtempofwftemps><objrights></objrights>"
                "<wfrefdoctempofwftemps></wfrefdoctempofwftemps><wfobjtempinsofwftemps></wfobjtempinsofwftemps>"
                "<wftempsubjectofwftemps></wftempsubjectofwftemps>")
        return await self.call("wfman.jsp", "cpcwftemp", "search", data)

    async def wf_wftemp_select(self, wftempid: str, objid: str, objtype: str) -> str:
        """进入编制界面（wfman cpcwftemp select，抓包实证）：
        请求体 = 模板全字段模板 + <wfobjs>{objid}_{objtype}</wfobjs> + stat=2。
        响应 = 模板主记录 + wfproctempofwftemps（节点模板，含 posx/posy/canmodify/operright/
        allperson/minperson/procuserstemp 执行人模板）+ proccondtempofwftemps（箭头模板
        fromprocid/toprocid/isdefault）。编制表单本地渲染，不发创建请求。"""
        data = ("<wftempid>%s</wftempid><wftempname></wftempname><rev>0</rev><code></code><codeid>0</codeid>"
                "<clas>0</clas><stat>2</stat><managers></managers><actived>0</actived><submitctrl>0</submitctrl>"
                "<period>0</period><breakprocid>0</breakprocid><keephistory>0</keephistory><canselect>0</canselect>"
                "<creator></creator><createtime></createtime><updator></updator><updatetime></updatetime>"
                "<updatereason></updatereason><publishor></publishor><publishtime></publishtime>"
                "<publishstage>0</publishstage><publishstatus>0</publishstatus><issecret>0</issecret><note></note>"
                "<comporgid>0</comporgid><comporgname></comporgname><breakfunc></breakfunc><modifyperiod>0</modifyperiod>"
                "<schedulerid>0</schedulerid><schedulerdesc></schedulerdesc><objaccess>0</objaccess><isupdateright>0</isupdateright>"
                "<parentid></parentid><parenttype></parenttype><objid>0</objid><objtype>0</objtype><namepath></namepath>"
                "<codename></codename><maxsearchrltcmt>0</maxsearchrltcmt><sqlwhere></sqlwhere>"
                "<sysbuiltinobj>0</sysbuiltinobj><canmodifytime>0</canmodifytime><idpath></idpath><typepath></typepath>"
                "<showtypeflag>0</showtypeflag><wfobjs>%s_%s</wfobjs><autosubmitmode>0</autosubmitmode>"
                "<comisnotemailto>0</comisnotemailto><wfnewmark>0</wfnewmark><autosubmitfirst>0</autosubmitfirst>"
                "<wftype>0</wftype><openmode>0</openmode><oneobjshow>0</oneobjshow><detaildispmode>0</detaildispmode>"
                "<prerun>0</prerun><objdefprops></objdefprops><objcrosss></objcrosss><wfproctempofwftemps></wfproctempofwftemps>"
                "<wfobjtempofwftemps></wfobjtempofwftemps><wftempofwftemps></wftempofwftemps>"
                "<proccondtempofwftemps></proccondtempofwftemps><objrights></objrights>"
                "<wfrefdoctempofwftemps></wfrefdoctempofwftemps><wfobjtempinsofwftemps></wfobjtempinsofwftemps>"
                "<wftempsubjectofwftemps></wftempsubjectofwftemps>" % (wftempid, objid, objtype))
        return await self.call("wfman.jsp", "cpcwftemp", "select", data)

    async def wf_create_from_template(self, wftempid: str, objid: str = "", objtype: str = "",
                                      objname: str = "", objrev: str = "", objrevlabel: str = "",
                                      executors: dict = None, node_flags: dict = None,
                                      opinion: str = "同意", subject: str = "",
                                      objects: list = None) -> dict:
        """创建流程（wfman cpcwf insert，2026-09-02 抓包 file_wf_create_v1 + wf_launch_dialog_v2 实证）。

        策略（PROTOCOL.md 建议）：不从零拼 33KB XML，而是把 cpcwftemp.select 响应
        改造后原样 POST insert，服务端按标签名取值：
          1) 主记录：wfid=0、stat=0、currprocid=0、flag=2、submitctrl=2、keephistory=2、
             priority=1、prerun=1、autosubmitmode=1、objid/objtype=0；
             subject：传入（用户编制期手改）→ 直填主记录，服务端原样保留
             （subjecttype=0，wf_launch_dialog_v2 实证「老天爷」）；留空 → 不填
             （服务端自动用首个对象名生成 subjecttype=6）。请求体无 subjecttype 标签。
          2) 节点：wfproctempofwftemps→wfprocofwfs、proctempid→procid、proctempname→procname、
             procusertempofwfproctemps→procuserofwfprocs（执行人，userid=流程启动者→当前用户）、
             wfuserrangeofwfproctemps→wfuserrangeofwfprocs、wfproctempnoticeofwfproctemps→wfprocnoticeofwfprocs、
             wfprocsigntempofwfproctemps→wfprocsignofwfprocs，并补 12 个空子表壳
          3) 箭头：proccondtempofwftemps→proccondofwfs
          4) wfobjofwfs：多对象支持（2026-09-02 wf_launch_dialog_v2 实证 5 对象 seq=1..N 一次
             挂载；objname 只传文件名即可，服务端 cpcwf select 回显自动补全路径）。
             objects=[{objid,objtype,objname,objrev,objrevlabel}, ...]（seq 从 1 递增；
             为空时回退单对象 objid/objtype/objname/... 参数，保持旧调用兼容）
          5) 顶层补空子表壳（wfsheetofwfs/wfsubjectofwfs/wftempofwfs/wfattachofwfs/wfrefofwfs/
             wfexportofwfs/wfrefdoctempofwfs/wfuserprivateofwfs/wfobjtempinsofwfs）

        executors: {procid: [{userid, username, ismajor}]}，缺省按模板自动：
        每个 proctype∈{3,4,5} 且 canmodify=2 的节点 → 模板 procusertemp 解析
        （流程启动者→当前用户）；模板为空 → **procid=1（编制/申请=启动者节点）强制填
        当前用户（ismajor=0、行procid=0，2026-09-02 wf_create_877_v1 抓包实证），
        其他节点留空不填执行人**（2026-09-02 用户要求取消"留空自动填当前用户"）。
        executors 行内 ismajor=2 = 决策人（同一节点仅一人，与 cpcwfproc.update 同语义）。
        proctype=7(发布)/开始/结束不填。

        node_flags: {procid: {allperson: "1"|"2"}}（可选，2026-09-02 用户需求：编制界面
        支持设置"所有人=全签"）——只覆盖**用户改过**的节点；未传入的节点保持模板原值
        （461 抓包实证 insert 原样透传模板 allperson=2，勿默认覆盖模板配置）。
        allperson: 1=任一执行人通过即完成，2=所有执行人都通过才算完成。

        返回 {"wfid": str, "wfname": str, "subject": str, "stat": str, "currprocid": str, "xml": 响应}
        """
        import datetime
        import xml.etree.ElementTree as _ET
        # ---- 对象列表合并（objects 优先；空则回退单对象参数，兼容旧调用） ----
        if objects:
            obj_items = [dict(o) for o in objects
                         if str((o or {}).get("objid", "") or "").strip()]
        else:
            obj_items = [dict(objid=objid, objtype=objtype, objname=objname or "",
                              objrev=objrev or "", objrevlabel=objrevlabel or "")]
            if not str(obj_items[0].get("objid") or "").strip():
                raise ValueError("缺少挂载对象：objid 为空且 objects 未提供")
        first = obj_items[0]
        sel = await self.wf_wftemp_select(wftempid, first.get("objid"), first.get("objtype"))
        try:
            root = _ET.fromstring(sel)
        except Exception as e:
            raise RuntimeError(f"cpcwftemp.select 响应解析失败: {e}")
        data = root.find("data")
        if data is None:
            raise RuntimeError("cpcwftemp.select 响应缺 <data>")
        user = self._me()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        def _set(el, tag, val):
            el2 = el.find(tag)
            if el2 is None:
                el2 = _ET.SubElement(el, tag)
            el2.text = str(val)
        def _del_children_keep_tag(el):
            for c in list(el):
                el.remove(c)
        # ---- 1) 主记录归零/置默认 ----
        for tag, val in (("wfid", 0), ("currprocid", 0), ("stat", 0), ("flag", 2),
                         ("submitctrl", 2), ("keephistory", 2), ("priority", 1),
                         ("prerun", 1), ("autosubmitmode", 1), ("objid", 0), ("objtype", 0),
                         ("subject", subject or ""), ("newcount", 0), ("currdelaycount", 0),
                         ("startdelaycount", 0), ("isread", 0), ("canmodifytime", 0),
                         ("isecwf", 0), ("maxsearchrltcmt", 0), ("shtinsid", 0),
                         ("shtid", 0), ("shttype", 0), ("wfnewmark", 0), ("urgeflag", 0),
                         ("wftype", 0), ("openmode", 0), ("oneobjshow", 0), ("detaildispmode", 0)):
            _set(data, tag, val)
        # wfobjs 是 select 专属字段，insert 不需要（对象走 wfobjofwfs 子表）
        wfobjs = data.find("wfobjs")
        if wfobjs is not None:
            data.remove(wfobjs)
        # 主记录 wfname = 模板名（wftempname）；insert 用 wfname 不用 wftempname
        wtn = data.find("wftempname")
        if wtn is not None:
            _set(data, "wfname", wtn.text or "")
            data.remove(wtn)
        # 模板主记录里 select 特有字段剔除（insert 里无对应语义，留着会被服务端忽略，
        # 但为贴近原版请求体，一并删掉 select 独有项）
        for tag in ("updatereason", "publishor", "publishtime", "publishstage", "publishstatus",
                    "canselect", "creator", "createtime", "updator", "updatetime", "idpath",
                    "typepath", "modifyperiod", "canmodifytime"):
            el = data.find(tag)
            if el is not None:
                data.remove(el)
        # ---- 2) 节点表改造 ----
        proc_tbl = data.find("wfproctempofwftemps")
        if proc_tbl is None:
            proc_tbl = _ET.SubElement(data, "wfproctempofwftemps")
        proc_tbl.tag = "wfprocofwfs"
        # insert 需要的节点空子表壳（缺一可能被服务端判结构不完整）
        PROC_EMPTY_SUBS = ("useropinionofwfprocs", "wfpublishofwfprocs", "taskofwfprocs",
                           "wfobjofwfprocs", "wfuserrangeofwfprocs", "wfprocnoticeofwfprocs",
                           "wfprocsignofwfprocs", "wfnodecollectionofwfprocs", "wfnodepathofwfprocs",
                           "wfprocfuncofwfprocs", "wfprocfuncparameterofwfprocs")
        exec_map = executors or {}
        for node in list(proc_tbl):
            if node.tag != "wfproctempofwftemp":
                continue
            node.tag = "wfprocofwf"
            # proctempid→procid、proctempname→procname
            ptid = node.find("proctempid")
            if ptid is not None:
                ptid.tag = "procid"
            ptn = node.find("proctempname")
            if ptn is not None:
                ptn.tag = "procname"
            node.find("wfid") if node.find("wfid") is not None else _set(node, "wfid", 0)
            node.find("procid") if node.find("procid") is not None else _set(node, "procid", "0")
            node.find("wfname") if node.find("wfname") is not None else _set(node, "wfname", "")
            # 子表改名：procusertemp→procuser；range；notice；sign
            for old, new in (("procusertempofwfproctemps", "procuserofwfprocs"),
                             ("wfuserrangeofwfproctemps", "wfuserrangeofwfprocs"),
                             ("wfproctempnoticeofwfproctemps", "wfprocnoticeofwfprocs"),
                             ("wfprocsigntempofwfproctemps", "wfprocsignofwfprocs")):
                el = node.find(old)
                if el is not None:
                    el.tag = new
                    for row in list(el):
                        if old == "procusertempofwfproctemps" and row.tag == "procusertempofwfproctemp":
                            row.tag = "procuserofwfproc"
                        elif old == "wfuserrangeofwfproctemps" and row.tag == "wfuserrangeofwfproctemp":
                            row.tag = "wfuserrangeofwfproc"
            # 执行人：前端覆盖（含空数组=显式留空，2026-09-02 取消自动填当前用户）> 模板自动
            pid_el = node.find("procid")
            pid = pid_el.text.strip() if pid_el is not None and pid_el.text else "0"
            ptype_el = node.find("proctype")
            ptype = ptype_el.text.strip() if ptype_el is not None and ptype_el.text else ""
            canmod_el = node.find("canmodify")
            canmod = canmod_el.text.strip() if canmod_el is not None and canmod_el.text else "0"
            pu_tbl = node.find("procuserofwfprocs")
            if pu_tbl is None:
                pu_tbl = _ET.SubElement(node, "procuserofwfprocs")
            need_user = (ptype in ("3", "4", "5")) and canmod == "2" and pid not in ("0", "99999")
            if pid in exec_map:
                # 前端显式提供（空数组=用户清空留空）→ 完全用前端值，清掉模板行
                _del_children_keep_tag(pu_tbl)
                for u in exec_map[pid]:
                    uid = str(u.get("userid", "") or "")
                    if not uid:
                        continue
                    row = _ET.SubElement(pu_tbl, "procuserofwfproc")
                    _set(row, "wfid", 0)
                    _set(row, "procid", pid)
                    _set(row, "userid", uid)
                    _set(row, "username", str(u.get("username", "") or uid))
                    _set(row, "ismajor", str(u.get("ismajor", "1") or "1"))
                    _set(row, "stat", 0)
                    _set(row, "rate", str(u.get("rate", "1") or "1"))
                    _set(row, "freq", 0)
                    _set(row, "issigned", 0)
                    _set(row, "tempuser", str(u.get("tempuser", "0") or "0"))
                    _set(row, "isread", 0)
                    _set(row, "goout", 0)
                    _set(row, "emptyset", "1")
                    _set(row, "syspositionid", 0)
                    _set(row, "iscommunicate", 0)
            else:
                # 模板自动：proctype∈{3,4,5} 且 canmodify=2 → 有模板执行人则保留（流程启动者→当前用户）；
                # 模板为空 → 留空不填（2026-09-02 取消"留空自动填当前用户"，仅对 procid=1 例外见下）
                if need_user:
                    rows = list(pu_tbl)
                    if rows:
                        for row in rows:
                            uid_el = row.find("userid")
                            uid = uid_el.text.strip() if uid_el is not None and uid_el.text else ""
                            if uid == "流程启动者":
                                if uid_el is not None:
                                    uid_el.text = user
                                un_el = row.find("username")
                                if un_el is not None:
                                    un_el.text = user
                            # 补 wfid/procid 等实例字段（模板行只有 wftempid/proctempid）
                            for rtag in ("wfid", "procid"):
                                if row.find(rtag) is None:
                                    _set(row, rtag, 0 if rtag == "wfid" else pid)
                            row.find("wftempid") if False else None
                            wt = row.find("wftempid")
                            if wt is not None:
                                row.remove(wt)
                            pt = row.find("proctempid")
                            if pt is not None:
                                row.remove(pt)
                            for rtag, rval in (("stat", 0), ("freq", 0), ("issigned", 0),
                                               ("isread", 0), ("goout", 0), ("syspositionid", 0),
                                               ("iscommunicate", 0)):
                                if row.find(rtag) is None:
                                    _set(row, rtag, rval)
                            if row.find("tempuser") is None:
                                _set(row, "tempuser", "2")
                            if row.find("emptyset") is None:
                                _set(row, "emptyset", "1")
                            if row.find("rate") is None:
                                _set(row, "rate", "1")
                else:
                    _del_children_keep_tag(pu_tbl)
            # procid=1（编制/申请=启动者节点）强制有执行人 = 当前用户（ismajor=0 启动者行）
            # 实证（2026-09-02 wf_create_877_v1 + file_wf_create_v1）：742/877/461 三模板
            # procid=1 的 procusertemp 全为空，但原版客户端 insert 仍提交「当前用户」行，
            # 行特征：行procid=0 / ismajor=0 / rate=0 / tempuser=0 / emptyset=0
            if pid == "1" and need_user and not list(pu_tbl):
                row = _ET.SubElement(pu_tbl, "procuserofwfproc")
                for tag, val in (("wfid", 0), ("procid", 0), ("userid", user), ("positionid", ""),
                                 ("agent", ""), ("stat", 0), ("rate", 0), ("freq", 0),
                                 ("issigned", 0), ("tempuser", 0), ("ismajor", 0), ("isread", 0),
                                 ("note", ""), ("username", user), ("orgpath", ""), ("agentname", ""),
                                 ("goout", 0), ("emptyset", 0), ("syspositionid", 0),
                                 ("iscommunicate", 0), ("addedbyuser", "")):
                    _set(row, tag, val)
            # 节点级完成条件覆盖（2026-09-02 用户需求：编制界面支持"所有人=全签"）。
            # node_flags 只含用户显式改过的节点（{pid: {allperson: "1"|"2"}}）；
            # 未在 node_flags 中的节点保留模板原值（461 insert 实证模板 allperson=2 原样透传）
            nf = (node_flags or {}).get(pid)
            if nf and str(nf.get("allperson", "")) in ("1", "2"):
                _set(node, "allperson", str(nf["allperson"]))
            # 补 12 个空子表壳（procuser 已在上面处理；这里补其余）
            for sub in PROC_EMPTY_SUBS:
                if node.find(sub) is None:
                    _ET.SubElement(node, sub)
            # 顶层 insert 需要但模板节点可能缺的数值字段
            for tag, val in (("period", 0), ("aperiod", 0), ("score", 0), ("width", 80),
                             ("height", 100), ("color", 0), ("flag", 0), ("submitcount", 0),
                             ("isdynamic", 0), ("needoutnotice", 1), ("isdefault", 0)):
                if node.find(tag) is None:
                    _set(node, tag, val)
        # ---- 3) 箭头表改造 ----
        cond_tbl = data.find("proccondtempofwftemps")
        if cond_tbl is not None:
            cond_tbl.tag = "proccondofwfs"
            for row in list(cond_tbl):
                if row.tag != "proccondtempofwftemp":
                    continue
                row.tag = "proccondofwf"
                if row.find("wfid") is None:
                    _set(row, "wfid", 0)
                wt = row.find("wftempid")
                if wt is not None:
                    row.remove(wt)
                # 补 fromprocname/toprocname（从节点表反查）
                if row.find("fromprocname") is None or row.find("toprocname") is None:
                    pass
        else:
            _ET.SubElement(data, "proccondofwfs")
        # ---- 4) wfobjofwfs（挂载对象：多对象 seq=1..N 递增，2026-09-02 wf_launch_dialog_v2 实证） ----
        obj_tbl = data.find("wfobjofwfs")
        if obj_tbl is None:
            obj_tbl = _ET.SubElement(data, "wfobjofwfs")
        else:
            _del_children_keep_tag(obj_tbl)
        # 与抓包 insert 行一致的字段序列（wfid=0；objname 只传文件名，服务端回显补路径）
        OBJ_ROW_FIELDS = (("wfid", 0), ("objid", ""), ("objtype", ""),
                          ("objname", ""), ("objcode", ""), ("objrev", ""),
                          ("objrevlabel", ""), ("tempobj", 0), ("seq", 0),
                          ("userid", ""), ("locked", 0), ("inttype", 0), ("parentid", ""),
                          ("parenttype", ""), ("projmanager", ""), ("projmanagername", ""),
                          ("fnuserid", ""), ("fnusername", ""), ("taskname", ""),
                          ("newseq", 0), ("archivetoid", ""), ("procid", 0), ("stat", 0),
                          ("archivetotype", ""), ("archivetoname", ""), ("parentpath", ""),
                          ("objstring1", ""), ("objstring2", ""), ("objstring3", ""),
                          ("objint1", 0), ("objint2", 0), ("objint3", 0), ("invorgid", 0),
                          ("bizobjcompbranch", ""), ("itemname", ""), ("itemdesc", ""),
                          ("itemcode", ""), ("drawid", ""), ("spec", ""), ("objurl", ""),
                          ("objtempname", ""), ("procacl", ""))
        for seq, o in enumerate(obj_items, start=1):
            o = dict(o)
            o.setdefault("objtype", objtype or "")
            o.setdefault("objname", objname or "")
            o.setdefault("objrev", objrev or "")
            o.setdefault("objrevlabel", objrevlabel or "")
            o["seq"] = seq
            row = _ET.SubElement(obj_tbl, "wfobjofwf")
            for tag, dflt in OBJ_ROW_FIELDS:
                _set(row, tag, o.get(tag, dflt))
        # ---- 5) 顶层空子表壳 ----
        for sub in ("wfsheetofwfs", "wfsubjectofwfs", "wftempofwfs", "wfattachofwfs",
                    "wfrefofwfs", "wfexportofwfs", "wfrefdoctempofwfs", "wfuserprivateofwfs",
                    "wfobjtempinsofwfs"):
            if data.find(sub) is None:
                _ET.SubElement(data, sub)
        # wfobjtempofwftemps 等模板专用顶层子表删除
        for sub in ("wfobjtempofwftemps", "wftempsubjectofwftemps", "wfrefdoctempofwftemps",
                    "wftempofwftemps", "wfobjtempinsofwftemps", "objdefprops", "objcrosss",
                    "objrights"):
            el = data.find(sub)
            if el is not None:
                data.remove(el)
        # ---- 序列化并发送 ----
        # call() 内部 build_body 会包 <Request><Data>…</Data></Request>（UTF-16LE），
        # 这里只传 <data> 的内部字段（不含 data 标签本身，避免大小写不匹配）。
        inner = "".join(_ET.tostring(ch, encoding="unicode") for ch in data)
        resp = await self.call("wfman.jsp", "cpcwf", "insert", inner)
        wfid = ""
        m = re.search(r"<wfid>([^<]*)</wfid>", resp)
        if m:
            wfid = m.group(1).strip()
        out = {"wfid": wfid, "xml": resp[:20000]}
        for tag in ("wfname", "subject", "stat", "currprocid", "startor"):
            mm = re.search(rf"<{tag}>([^<]*)</{tag}>", resp)
            if mm:
                out[tag] = mm.group(1)
        return out

    # ---- 已创建实例的对象管理（2026-09-02 wf_launch_dialog_v2 实证：
    #      cpcwfobj.delete 带完整对象行 → cpcwf.arrangeobjseq 重排剩余 seq） ----

    async def wf_obj_remove(self, wfid: str, objid: str, objtype: str = "6") -> dict:
        """从已创建流程实例删除对象并自动重排剩余对象（原版 REQ23→REQ24 连续动作）。

        原版 delete 请求体 = <wfid> + cpcwf.select 回显的**完整对象行**叶子字段平铺
        （objname 带完整路径 / parentid/parenttype / stat=1 / objstring3=审核中 /
        locked=1 / inttype=1 等，1271 字符 40 字段，非仅 wfid+objid）→ 故本方法先
        wf_detail 定位该对象行，取其叶子字段平铺构造 delete body（值 XML 转义）。
        删除成功后自动 arrangeobjseq 把剩余对象 seq 重排为 1..N 连续。

        返回 {"ok", "deleted": {objid, seq}, "remaining", "arrange"(末次响应头)}
        """
        from xml.sax.saxutils import escape as _xesc
        sel = await self.wf_detail(wfid)
        chunks = list(re.finditer(r"<wfobjofwf>(.*?)</wfobjofwf>", sel, re.S))
        chunk = None
        del_seq = ""
        remaining = []
        for mm in chunks:
            c = mm.group(1)
            m = re.search(r"<objid>([^<]*)</objid>", c)
            ms = re.search(r"<seq>([^<]*)</seq>", c)
            oid = m.group(1).strip() if m else ""
            sq = ms.group(1).strip() if ms else ""
            if oid == str(objid):
                chunk = c
                del_seq = sq
            else:
                if sq.isdigit():
                    remaining.append(int(sq))
        if chunk is None:
            raise ValueError(f"流程 {wfid} 中未找到对象 objid={objid} objtype={objtype}（请先刷新流程详情）")
        parts = [f"<wfid>{_xesc(str(wfid))}</wfid>"]
        for tag, val in re.findall(r"<([a-zA-Z_][a-zA-Z0-9_]*)>([^<]*)</\1>", chunk):
            if tag == "wfid":
                continue  # 行内 wfid 已有值（select 回显主记录），避免重复标签
            parts.append(f"<{tag}>{_xesc(val)}</{tag}>")
        resp = await self.call("wfman.jsp", "cpcwfobj", "delete", "".join(parts))
        # 删除成功 → 自动重排剩余（原版 delete 后紧接 arrangeobjseq，无中间 select）
        arrange_xml = ""
        if remaining:
            order = [{"seq": str(s), "newseq": str(i)}
                     for i, s in enumerate(sorted(remaining), start=1)]
            arrange_xml = await self.wf_obj_arrange(wfid, order)
        return {"ok": True, "deleted": {"objid": objid, "seq": del_seq},
                "remaining": len(remaining), "arrange": (arrange_xml or "")[:20000],
                "delete_xml": resp[:8000]}

    async def wf_obj_arrange(self, wfid: str, order: list) -> str:
        """重排流程实例对象顺序（wfman cpcwf arrangeobjseq，2026-09-02 实证）。

        order: [{"seq": 旧序号, "newseq": 新序号}, ...]（原版删对象后把剩余对象
        seq 重排为 1..N 连续；请求每行只填 seq→newseq，其余行字段 0/空）。
        请求体主记录 = cpcwf 全字段模板（WF_SELECT 与抓包 arrange 主记录 72 字段
        逐一 diff 一致，仅 wfid 有值）→ 替换 <wfobjofwfs> 空壳为 seq/newseq 行。

        返回 arrange 响应 XML（回显行 objid=0、seq→newseq）。
        """
        data = _fill_template(WF_SELECT, {"wfid": wfid})
        rows = []
        for o in (order or []):
            seq, newseq = str(o.get("seq", "")), str(o.get("newseq", ""))
            rows.append(
                "<wfobjofwf><wfid>0</wfid><objid>0</objid><objtype>0</objtype>"
                "<objname></objname><objcode></objcode><objrev>0</objrev><objrevlabel></objrevlabel>"
                f"<tempobj>0</tempobj><seq>{seq}</seq><userid></userid><locked>0</locked>"
                "<inttype>0</inttype><parentid></parentid><parenttype></parenttype>"
                "<projmanager></projmanager><projmanagername></projmanagername>"
                "<fnuserid></fnuserid><fnusername></fnusername><taskname></taskname>"
                f"<newseq>{newseq}</newseq><archivetoid></archivetoid><procid>0</procid><stat>0</stat>"
                "<archivetotype></archivetotype><archivetoname></archivetoname>"
                "<parentpath></parentpath><objstring1></objstring1><objstring2></objstring2>"
                "<objstring3></objstring3><objint1>0</objint1><objint2>0</objint2>"
                "<objint3>0</objint3><invorgid>0</invorgid><bizobjcompbranch></bizobjcompbranch>"
                "<itemname></itemname><itemdesc></itemdesc><itemcode></itemcode><drawid></drawid>"
                "<spec></spec><objurl></objurl><objtempname></objtempname><procacl></procacl>"
                "</wfobjofwf>")
        data = re.sub(r"<wfobjofwfs></wfobjofwfs>",
                      f"<wfobjofwfs>{''.join(rows)}</wfobjofwfs>", data, count=1)
        return await self.call("wfman.jsp", "cpcwf", "arrangeobjseq", data)

    async def wf_obj_add(self, wfid: str, docs: list) -> dict:
        """向已创建流程实例追加对象（2026-09-03 wf_detail_add_obj_v1/v2 实证）。

        原版在流程执行窗口「关联对象」区点添加 → 目录浏览（cpcfdr selectref）→
        勾选文档 → `wfman cpcwfobj insert`：请求体 42 标签**轻量行**
        （objid/objtype=6/objname 纯文件名/objrev/objrevlabel/tempobj=0/seq/locked=0/
        inttype=0/procid=0/stat=0，parentid/parenttype 空，余 0/空）——
        非 cpcwf.insert 多对象子表、非 delete 完整行（v1 REQ6 与 v2 REQ5 逐字同构仅
        objid/objname/seq 不同）。

        seq = 客户端自取 = 现有对象最大 seq + 1（两次实证：空流程 0→1、已有 seq=1 →
        2），服务端回显同值不自行分配。**追加后无需 arrangeobjseq**（仅删除要）。
        ⚠ 一次勾选 N 个的请求形态未直接实证（v1/v2 均单 insert），此处按行语义
        对每个对象发独立 insert 串行（seq 连续递增）。

        docs: [{objid, objtype="6", objname, objrev, objrevlabel}]（objname 只传文件名）
        返回 {"ok", "added": [{objid, seq, resp_xml_head}...], "max_seq"}
        """
        from xml.sax.saxutils import escape as _xesc
        # 1) 取现有对象最大 seq（wf_detail 的 select 回显 wfobjofwfs 行，行内 <seq>）
        sel = await self.wf_detail(wfid)
        max_seq = 0
        for mm in re.finditer(r"<wfobjofwf>(.*?)</wfobjofwf>", sel, re.S):
            m = re.search(r"<seq>([^<]*)</seq>", mm.group(1))
            if m and m.group(1).strip().isdigit():
                max_seq = max(max_seq, int(m.group(1).strip()))
        # 2) 逐对象 insert（请求体 42 标签与 OBJ_ROW_FIELDS / 抓包 insert 行一致；
        #    标签顺序无关紧要，此处按抓包原文序列构造）
        def _row(doc: dict, seq: int) -> str:
            return ("<wfid>%s</wfid>" % _xesc(str(wfid))) + \
                   ("<objid>%s</objid>" % _xesc(str(doc.get("objid", "")))) + \
                   ("<objtype>%s</objtype>" % _xesc(str(doc.get("objtype", "6")))) + \
                   ("<objname>%s</objname>" % _xesc(str(doc.get("objname", "")))) + \
                   "<objcode></objcode>" + \
                   ("<objrev>%s</objrev>" % _xesc(str(doc.get("objrev", "")))) + \
                   ("<objrevlabel>%s</objrevlabel>" % _xesc(str(doc.get("objrevlabel", "")))) + \
                   "<tempobj>0</tempobj>" + \
                   ("<seq>%d</seq>" % seq) + \
                   "<userid></userid><locked>0</locked><inttype>0</inttype>" \
                   "<parentid></parentid><parenttype></parenttype>" \
                   "<projmanager></projmanager><projmanagername></projmanagername>" \
                   "<fnuserid></fnuserid><fnusername></fnusername><taskname></taskname>" \
                   "<newseq>0</newseq><archivetoid></archivetoid>" \
                   "<procid>0</procid><stat>0</stat>" \
                   "<archivetotype></archivetotype><archivetoname></archivetoname><parentpath></parentpath>" \
                   "<objstring1></objstring1><objstring2></objstring2><objstring3></objstring3>" \
                   "<objint1>0</objint1><objint2>0</objint2><objint3>0</objint3><invorgid>0</invorgid>" \
                   "<bizobjcompbranch></bizobjcompbranch><itemname></itemname><itemdesc></itemdesc>" \
                   "<itemcode></itemcode><drawid></drawid><spec></spec><objurl></objurl>" \
                   "<objtempname></objtempname><procacl></procacl>"
        added = []
        for i, doc in enumerate(docs or [], start=1):
            seq = max_seq + i
            resp = await self.call("wfman.jsp", "cpcwfobj", "insert", _row(doc, seq))
            added.append({"objid": str(doc.get("objid", "")), "seq": seq,
                          "resp_xml_head": resp[:2000]})
        return {"ok": True, "added": added, "max_seq": max_seq,
                "wfid": str(wfid), "count": len(added)}


    async def ecn_select(self, ecnid: str) -> str:
        """ECN 详情（更改通知单）。2026-08-20 抓包实证：
        POST /jsp/ecmman.jsp?classid=cpcecn&action=select
        请求体 = 完整 ECN 字段模板（仅 ecnid 有效，其余留空）。
        """
        data = _fill_template(ECN_SELECT_TEMPLATE, {"ecnid": ecnid})
        return await self.call("ecmman.jsp", "cpcecn", "select", data)

    async def ecn_cm_searchdata(self, invorgid: str = "104") -> str:
        """ECN 变更明细（cpcecncm.searchdata，抓包实证）。"""
        data = (f"<ecncmid>0</ecncmid><ecntypeid>1</ecntypeid><ecntypename></ecntypename>"
                f"<invorgid>{invorgid}</invorgid><invorgname></invorgname>"
                f"<sqlwhere></sqlwhere><maxsearchrltcmt>0</maxsearchrltcmt>"
                f"<cpcecncmcdataofcpcecncms></cpcecncmcdataofcpcecncms>"
                f"<cpcecncmdefpropsetofcpcecncms></cpcecncmdefpropsetofcpcecncms>"
                f"<cpcecncmrptofcpcecncms></cpcecncmrptofcpcecncms>"
                f"<cpcecnoperationofcpcecncms></cpcecnoperationofcpcecncms>"
                f"<cpcecncmitemoprationofcpcecncms></cpcecncmitemoprationofcpcecncms>"
                f"<cpcecncmrelationofcpcecncms></cpcecncmrelationofcpcecncms>"
                f"<cpcecncmdrawconfofcpcecncms></cpcecncmdrawconfofcpcecncms>")
        return await self.call("ecmman.jsp", "cpcecncm", "searchdata", data)

    async def samconfirm_select(self, samcon_id: str) -> str:
        """物料确认单(SamConfirm)详情。2026-08-20 抓包实证：
        POST /jsp/ecmman.jsp?classid=cpcecnsamconfirm&action=select
        请求体 = 完整确认单字段模板（仅 samcon_id 有效，其余留空）。
        响应含子表 cpcecnsamconfirmitemsofecnsamconfirms（确认物料项）。
        """
        data = _fill_template(SAMCONFIRM_SELECT_TEMPLATE, {"samcon_id": samcon_id})
        return await self.call("ecmman.jsp", "cpcecnsamconfirm", "select", data)

    async def pdtbatchapply_select(self, applyid: str) -> str:
        """产品投产通知单(PDTBatchApply)详情。2026-08-31 抓包实证（prodlaunch_popup_v1）：
        POST /jsp/ecmman.jsp?classid=cpcpdtbatchapply&action=select
        请求体 = 完整字段模板（仅 applyid 有效，其余留空）。
        响应 = 主表字段（applyid/applycode/code 项目号/projname 项目名称/projphasename 项目阶段/
        manager 项目经理/creator…）+ 子表 pdtbatchapplylineofpdtbatchapplys（投产产品清单，
        实测 BA2608270001 → 22 行，每行含 itemcode/itemname/productmodel/marketmodel/
        custommodel/prodlifecycle/iscert/iserpbatch/ishasrep/prodform 等）。
        objtype=202204（产品投产通知单/流程单号类）时前端调用此接口。
        """
        data = _fill_template(PDTBATCHAPPLY_SELECT_TEMPLATE, {"applyid": applyid})
        return await self.call("ecmman.jsp", "cpcpdtbatchapply", "select", data)

    async def item_select(self, itemid: str) -> str:
        """物料明细（客户端右键「打开明细表」，2026-08-24 抓包实证）：
        bomman.jsp?classid=cpcitem&action=select，请求体精简 <itemid> 即可
        （实测返回 191KB / 339 字段完整物料主数据，含 itemname/itemdesc/
        drawid/rev/note/creator/weight/drawsize 等）。"""
        data = f"<itemid>{itemid}</itemid>"
        return await self.call("bomman.jsp", "cpcitem", "select", data)

    async def bom_select(self, asmid: str, rev: str = "", itemcode: str = "") -> str:
        """BOM 明细表主接口（2026-08-24 实证：客户端明细表窗口 = bomman.jsp cpcbom.select）。

        关键：必须带正确 asmrev（= 父物料 rev），否则服务端返回空 bomofbom/无 <bomds>。
        返回响应含 <bomds><bomd>...</bomd> 子件列表（每个 bomd 81 字段，比 selectref 完整：
        materialcatname 研发分类 / eptag 环保标识 / hasrefdoc 包含文件 / lifecycle /
        costrollup / bomddef6-10 / saleunit/purchunit/stockunit / basetime/ctrltime 等）。
        """
        data = _fill_template(BOM_SELECTREF, {
            "asmid": asmid, "invorgid": "104",
            "itemcode": itemcode, "asmrev": rev or "",
        })
        return await self.call("bomman.jsp", "cpcbom", "select", data)

    async def bomrpt_getbom(self, itemid: str, itemcode: str, itemrev: str = "",
                            maxlayer: str = "0", invorgid: str = "104") -> str:
        """多层明细（BOM 报表）：bomman.jsp cpcbomrpt.getbom（2026-08-24 抓包实证 prod_multilevel_capture）。

        一次返回整棵 BOM 树（响应 maxlayer=15，579 行 <bomd>，2MB），每行带：
        wbs（层级路径 0/1/1.1…）、asmcode/asmrev（父）、itemcode/itemname/qty/itemunit/
        purchflag/clas/prodlifecycle/lifecycle/actived/materialcatname(研发分类)/
        basetime/ctrltime/ecnid/ecncode/exptime/hasrefdoc/rev/locked 等。
        maxlayer=0 表示全部层级（客户端抓包即 0）。
        """
        data = (
            f"<invorgid>{invorgid}</invorgid><itemcode>{itemcode}</itemcode><itemid>{itemid}</itemid>"
            f"<itemrev>{itemrev}</itemrev><itemname></itemname><maxlayer>{maxlayer}</maxlayer>"
            f"<reffields>wbs,bomlevel,seq,itemcode,itemname,drawid,spec,material,productmodel,custommodel,marketmodel,location,itemunit,qty,note,purchflag,clas,prodlifecycle,lifecycle,actived,kinds,bomdef3,itemdesc,itemename,itemkind,eptag,itemlevel,importance,itemdescbak,basetime,ctrltime,ecnid,ecncode,exptime,hasrefdoc,itemrev,locked,wfid,rev,keycid</reffields>"
            f"<parfields></parfields><orderby></orderby><datapacketflag>1</datapacketflag><mrpdsid>0</mrpdsid>"
            f"<bomid>0</bomid><asmid>0</asmid><bomviewid>0</bomviewid><bomviewaltsuid>0</bomviewaltsuid>"
            f"<itemdesc></itemdesc><drawid></drawid><evacosttime></evacosttime><evacosttranflag>0</evacosttranflag>"
            f"<bpdtsearch>False</bpdtsearch><basetimestart></basetimestart><basetimeend></basetimeend>"
            f"<productcount>0</productcount><flag>0</flag><destinvorgid>0</destinvorgid><borrowid>0</borrowid>"
            f"<searchflag>0</searchflag><altype>0</altype><altitems></altitems><strdef8></strdef8>"
            f"<itemdef1></itemdef1><itemdef2></itemdef2><spec></spec><replacegrtype>2</replacegrtype>"
            f"<certtype></certtype><saletype></saletype><showprinted>0</showprinted><searchtype>0</searchtype>"
            f"<bomds></bomds><itemborrowbomchangeofbomrpts></itemborrowbomchangeofbomrpts><itemborrowitemchangeofbomrpts></itemborrowitemchangeofbomrpts>"
        )
        return await self.call("bomman.jsp", "cpcbomrpt", "getbom", data)

    async def samcm_searchdata(self, samcmid: str, invorgid: str = "104") -> str:
        """物料确认单明细(cpcecnsamcm.searchdata，抓包实证）。
        samcmid=0 查全部；=具体 id 查该确认单明细行。
        """
        data = (f"<samcmid>{samcmid}</samcmid><invorgid>{invorgid}</invorgid>"
                f"<invorgname></invorgname><creator></creator><createtime></createtime>"
                f"<updator></updator><updatetime></updatetime><sqlwhere></sqlwhere>"
                f"<helpfilename></helpfilename><helpfileid>0</helpfileid><maxsearchrltcmt>0</maxsearchrltcmt>"
                f"<expiremintimes>0</expiremintimes><expiremaxtimes>0</expiremaxtimes>"
                f"<pauseexpiretimes>0</pauseexpiretimes><remindcycle></remindcycle><mailuser></mailuser>"
                f"<samconrepealproj>0</samconrepealproj>"
                f"<ecnsamdefpropsetofecnsamcms></ecnsamdefpropsetofecnsamcms>"
                f"<ecnsamrptofecnsamcms></ecnsamrptofecnsamcms>")
        return await self.call("ecmman.jsp", "cpcecnsamcm", "searchdata", data)

    async def wf_proc_info(self, wfid: str, procid: str) -> str:
        data = _fill_template(WF_GETSUBMITNODEINFO, {"wfid": wfid, "procid": procid})
        return await self.call("wfman.jsp", "cpcwfproc", "getsubmitnodeinfo", data)

    async def wf_submit(self, wfid: str, procid: str, opinion: str = "同意", procname: str = "") -> str:
        fields = {"wfid": wfid, "procid": procid, "opinion": opinion}
        if procname:
            fields["procname"] = procname
        data = _fill_template(WF_SUBMIT, fields)
        return await self.call("wfman.jsp", "cpcwfproc", "submit", data)

    async def wf_submit_dynamic(self, wfid: str, procid: str, opinion: str, procname: str = "", submitto: str = "") -> str:
        """2026-08-21 修复：动态构造提交（通过）请求。

        根因：旧 WF_SUBMIT 是固定模板（wfid=1328200 别的流程），带 arrivefunc 脚本、
        缺 flag=6，服务端执行后把主流程 stat 直接改成 4（结束）→ 流程被推到 99999。
        原生 submit body（wfid=1329815 会签/申请，v2_submit_1.txt）特征：
          - 顶层 stat=4、flag=6、arrivefunc/submitfunc/rejectfunc 全部为空
          - useropinionofwfproc stat=7（已完成）、procuser freq=1/issigned=1/stat=0
          - wfnodepath 为「当前节点向后」的链（如 1-2-3）
        本方法与 wf_reject_dynamic 同构：select 取 chunk → 改顶层意见/stat/flag →
        重建 useropinion（stat=7）→ 修正 procuser → 调 cpcwfproc.submit。
        """
        import datetime
        import re as _re
        sel = await self.wf_detail(wfid)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        procid_s = str(procid).strip()
        chunk = None
        for m in _re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", sel, _re.S):
            c = m.group(1)
            if _re.search(r"<procid>\s*" + _re.escape(procid_s) + r"\s*</procid>", c):
                chunk = c
                break
        if not chunk:
            raise RuntimeError(f"wf_submit_dynamic: wfid={wfid} 未找到 procid={procid_s}")

        def _g(tag):
            mm = _re.search(rf"<{tag}>([^<]*)</{tag}>", chunk)
            return mm.group(1) if mm else ""

        # 剔除 select 特有字段
        for tag in ("nextstat", "alarmtime", "autosubmittime", "startor", "sourceuserid"):
            chunk = _re.sub(rf"<{tag}>[^<]*</{tag}>", "", chunk)
        chunk = _re.sub(r"<freq>[^<]*</freq>(?=[\s\S]*<procuserofwfprocs>)", "", chunk)
        pendtime = _g("pendtime") or ""
        astatime = _g("astatime") or now
        user = self._me()
        # 重建 useropinionofwfprocs（submit：stat=7 已完成）
        new_op = (f"<useropinionofwfprocs><useropinionofwfproc><wfid>{wfid}</wfid>"
                  f"<procid>{procid_s}</procid><procname></procname><userid>{user}</userid>"
                  f"<username>{user}</username><freq>1</freq><positionid></positionid>"
                  f"<stat>7</stat><isagent>1</isagent><signtime>{now}</signtime>"
                  f"<opinion>{opinion}</opinion><accopinion></accopinion>"
                  f"<duetime>{pendtime}</duetime><period>0</period>"
                  f"<syspositionid>0</syspositionid><attaches></attaches><attachnames></attachnames>"
                  f"<astatime>{astatime}</astatime></useropinionofwfproc></useropinionofwfprocs>")
        chunk = _re.sub(r"<useropinionofwfprocs>.*?</useropinionofwfprocs>", new_op, chunk, flags=_re.S)
        # 计算所有过程节点（用于确定提交目标/前向链）
        procs = {}
        for m in _re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", sel, _re.S):
            c = m.group(1)
            pid = _re.search(r"<procid>([^<]*)</procid>", c)
            if pid and pid.group(1).strip() not in ("99999", "0"):
                try:
                    procs[int(pid.group(1).strip())] = True
                except ValueError:
                    pass
        tail = sorted(procs)
        cur_i = int(procid_s)
        # 提交目标节点（原生 submit 用 rejectto 字段表达「提交到哪」）：
        #   指定 submitto → 用该值；缺省 → 当前节点之后第一个（默认下一节点）。
        #   对应原生样本(wfid=1329821): 节点1提交选节点3 → rejectto=3, nodepath=1-2-3, rejectnodes=3
        tgt = None
        if submitto:
            try:
                tgt = int(str(submitto).strip())
            except ValueError:
                tgt = None
        if tgt is None:
            nxt = [p for p in tail if p > cur_i]
            tgt = nxt[0] if nxt else cur_i
        # 前向链：current → ... → target（含中间节点），匹配原生 1-2-3
        ahead = [str(p) for p in tail if cur_i <= p <= tgt]
        nodepath = "-".join(ahead) if ahead else procid_s
        # 顶层 opinion + rejectto 无条件插最前（原生 submit 必含 rejectto=目标节点）
        for _tag in ("procuserofwfprocs", "useropinionofwfprocs", "wfuserrangeofwfprocs"):
            _idx = chunk.find("<" + _tag + ">")
            if _idx >= 0:
                _head = chunk[:_idx]
                _head = _re.sub(r"<opinion>[^<]*</opinion>", "", _head, count=1)
                _head = _re.sub(r"<rejectto>[^<]*</rejectto>", "", _head, count=1)
                chunk = _head + chunk[_idx:]
                break
        chunk = f"<opinion>{opinion}</opinion><rejectto>{tgt}</rejectto>" + chunk
        # 顶层 stat → 4；flag → 6（原生 submit flag=6）
        chunk = _re.sub(r"(<stat>)[^<]*(</stat>)", r"\g<1>4\g<2>", chunk, count=1)
        if _re.search(r"<flag>", chunk):
            chunk = _re.sub(r"(<flag>)[^<]*(</flag>)", r"\g<1>6\g<2>", chunk, count=1)
        else:
            chunk = chunk.replace("<width>", "<flag>6</flag><width>", 1)
        # 清空节点脚本（原生 submit 为空）
        for tag in ("arrivefunc", "submitfunc", "rejectfunc", "ctrlsubmitfunc", "clientsubfunc", "checkfunc"):
            chunk = _re.sub(rf"<{tag}>[^<]*</{tag}>", f"<{tag}></{tag}>", chunk, count=1)
        # 修正 procuser：freq=1 / issigned=1 / stat=0 / tempuser=0 / ismajor=0
        chunk = _re.sub(
            rf"(<procuserofwfproc>(?:(?!</procuserofwfproc>).)*?<userid>{_re.escape(user)}</userid>(?:(?!</procuserofwfproc>).)*?)(<issigned>)[^<]*(</issigned>)",
            lambda mm: mm.group(1) + mm.group(2) + "1" + mm.group(3), chunk, flags=_re.S, count=1)
        chunk = _re.sub(
            rf"(<procuserofwfproc>(?:(?!</procuserofwfproc>).)*?<userid>{_re.escape(user)}</userid>(?:(?!</procuserofwfproc>).)*?)(<freq>)[^<]*(</freq>)",
            lambda mm: mm.group(1) + mm.group(2) + "1" + mm.group(3), chunk, flags=_re.S, count=1)
        # wfnodepath：nodepath=前向链，rejectnodes=目标节点（原生 submit rejectnodes=目标）
        chunk += (f"<wfnodepathofwfprocs><wfnodepathofwfproc><wfid>0</wfid><procid>0</procid>"
                  f"<nodepath>{nodepath}</nodepath><unsubmitnodes></unsubmitnodes>"
                  f"<rejectnodes>{tgt}</rejectnodes></wfnodepathofwfproc></wfnodepathofwfprocs>")
        return await self.call("wfman.jsp", "cpcwfproc", "submit", chunk)

    async def wf_reject(self, wfid: str, procid: str, opinion: str = "驳回", procname: str = "") -> str:
        fields = {"wfid": wfid, "procid": procid, "opinion": opinion}
        if procname:
            fields["procname"] = procname
        data = _fill_template(WF_REJECT, fields)
        return await self.call("wfman.jsp", "cpcwfproc", "reject", data)

    async def wf_reject_dynamic(self, wfid: str, procid: str, opinion: str,
                                rejectto: str = "", procname: str = "") -> str:
        """方案A（2026-08-21 抓包修复）：动态构造真实驳回请求。

        根因：旧 WF_REJECT 是 submit 模板冒充、缺 <rejectto>，
              服务端把节点标驳回但找不到回退目标 → 按"完成"自动推进。
        真实抓包（wfid=1329808 两次驳回，rejectto=1/2）证实：
          reject 请求体 = 当前节点完整 proc 字段（含 wfuserrangeofwfprocs /
          procuserofwfprocs / useropinionofwfprocs / wfnodepathofwfprocs）+ <rejectto>目标节点。

        本方法：
          1) cpcwf.select 取当前流程 XML，提取目标 procid 的 <wfprocofwf> 完整块
          2) 剔除 select 特有字段（nextstat/freq/alarmtime/autosubmittime/startor/sourceuserid）
          3) 顶层 <opinion> 替换为用户意见；新增/替换 <rejectto>
          4) 重建 <useropinionofwfprocs>（当前用户单条：signtime=now, stat=5, isagent=1）
          5) 修正当前用户 <procuserofwfproc> 的 issigned=1/freq=1
          6) 按 preproc 链生成 <wfnodepathofwfproc> 的 nodepath（如 0-1-2-3）
          7) 调 cpcwfproc.reject
        """
        import datetime
        import re as _re
        sel = await self.wf_detail(wfid)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        procid_s = str(procid).strip()
        # 0) 收集所有节点的 preproc / stat，供默认 rejectto 计算（必须先于下方"默认 rejectto"逻辑引用，
        #    否则触发 UnboundLocalError: cannot access local variable 'procs_stat' where it is not associated with a value）
        procs = {}
        procs_stat = {}
        procs_time = {}
        conds = []  # (fromprocid, toprocid) 箭头拓扑（proccondofwf，与流程图箭头同源）
        for _m in _re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", sel, _re.S):
            _c = _m.group(1)
            _pid = _re.search(r"<procid>([^<]*)</procid>", _c)
            _pre = _re.search(r"<preproc>([^<]*)</preproc>", _c)
            _st = _re.search(r"<stat>([^<]*)</stat>", _c)
            _pt = _re.search(r"<pendtime>([^<]*)</pendtime>", _c)
            if _pid and _pre:
                procs[_pid.group(1).strip()] = _pre.group(1).strip()
                if _st:
                    procs_stat[_pid.group(1).strip()] = _st.group(1).strip()
                if _pt:
                    procs_time[_pid.group(1).strip()] = _pt.group(1).strip()
        for _m in _re.finditer(r"<proccondofwf>(.*?)</proccondofwf>", sel, _re.S):
            _f = _re.search(r"<fromprocid>([^<]*)</fromprocid>", _m.group(1))
            _t = _re.search(r"<toprocid>([^<]*)</toprocid>", _m.group(1))
            if _f and _t:
                conds.append((_f.group(1).strip(), _t.group(1).strip()))
        # 1) 提取目标 procid 完整块
        chunk = None
        for m in _re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", sel, _re.S):
            c = m.group(1)
            if _re.search(r"<procid>\s*" + _re.escape(procid_s) + r"\s*</procid>", c):
                chunk = c
                break
        if not chunk:
            raise RuntimeError(f"wf_reject_dynamic: wfid={wfid} 未找到 procid={procid_s}")

        def _g(tag):
            mm = _re.search(rf"<{tag}>([^<]*)</{tag}>", chunk)
            return mm.group(1) if mm else ""

        # 2) 剔除 select 特有字段（freq 用前瞻：只删顶层的，procuser/useropinion 子表里的要保留）
        for tag in ("nextstat", "alarmtime", "autosubmittime", "startor", "sourceuserid"):
            chunk = _re.sub(rf"<{tag}>[^<]*</{tag}>", "", chunk)
        chunk = _re.sub(r"<freq>[^<]*</freq>(?=[\s\S]*<procuserofwfprocs>)", "", chunk)
        # 3) 顶层 opinion 替换 + rejectto
        pendtime = _g("pendtime") or ""
        astatime = _g("astatime") or now
        user = self._me()
        # 3) 重建 useropinionofwfprocs（先做；这样后续处理顶层意见时不会误改子表）
        new_op = (f"<useropinionofwfprocs><useropinionofwfproc><wfid>{wfid}</wfid>"
                  f"<procid>{procid_s}</procid><procname></procname><userid>{user}</userid>"
                  f"<username>{user}</username><freq>1</freq><positionid></positionid>"
                  f"<stat>5</stat><isagent>1</isagent><signtime>{now}</signtime>"
                  f"<opinion>{opinion}</opinion><accopinion></accopinion>"
                  f"<duetime>{pendtime}</duetime><period>0.0167</period>"
                  f"<syspositionid>0</syspositionid><attaches></attaches><attachnames></attachnames>"
                  f"<astatime>{astatime}</astatime></useropinionofwfproc></useropinionofwfprocs>")
        chunk = _re.sub(r"<useropinionofwfprocs>.*?</useropinionofwfprocs>", new_op, chunk, flags=_re.S)
        # 4) 顶层 <opinion> + <rejectto>（无条件插到 chunk 最前，保证存在）：
        #    select 返回的 wfprocofwf chunk 顶层字段被 wfuserrangeofwfprocs 等子表切分
        #    （procid/procname/isskip 等顶层字段在子表之后），无法可靠按"第一个子表前"拆分。
        #    原生 body 顺序为 wfid/procid/procname/.../opinion/.../rejectto/...，XML 按标签名解析，
        #    插到最前不影响取值。若顶层已有 opinion（已完成节点），一并先删除避免歧义。
        if not rejectto:
            # 2026-09-02 修复（1330666 实证）：默认目标必须沿箭头拓扑（proccondofwf）取直系前驱。
            # 坑：多分枝模板里 <preproc> ≠ 图上箭头（1330666 品质会签(2).preproc=1，图上箭头是
            # 7-编制→2-品质会签）；旧兜底"全流程 stat=7 里 procid 最大"会命中平行分支的 17-编制，
            # 服务端判 17 非前驱无效后回退节点 preproc=1 → 流程驳到错误分支 1-编制。
            # 新规则：直系前驱中 stat=7 且 pendtime 最近者；无则逆向 BFS 全部上游中 stat=7 最近者；
            # 仍无（模板无 proccondofwf 数据）才退回旧 preproc 链逻辑。
            preds = [f for (f, t) in conds if t == procid_s and f not in ("0", "99999", procid_s)]
            passed_preds = [p for p in preds if procs_stat.get(p) == "7"]
            if passed_preds:
                rejectto = max(passed_preds, key=lambda p: procs_time.get(p, ""))
            else:
                _rev = {}
                for f, t in conds:
                    _rev.setdefault(t, []).append(f)
                _seen, _queue = {procid_s}, [procid_s]
                while _queue:
                    c = _queue.pop(0)
                    for f in _rev.get(c, []):
                        if f in ("0", "99999") or f in _seen:
                            continue
                        _seen.add(f)
                        _queue.append(f)
                up_passed = [p for p in _seen if p != procid_s and procs_stat.get(p) == "7"]
                if up_passed:
                    rejectto = max(up_passed, key=lambda p: procs_time.get(p, ""))
            if not rejectto:
                # 旧逻辑兜底（select 响应无 proccondofwf 的模板）
                rejectto = _g("preproc") or "0"
                best = ""
                node = str(rejectto)
                guard = 0
                while node and node in procs_stat and guard < 60:
                    if procs_stat.get(node) == "7" and node not in ("0", "99999"):
                        best = node
                        break
                    node = procs.get(node, "")
                    guard += 1
                if not best:
                    passed = [p for p, s in procs_stat.items() if s == "7" and p not in ("0", "99999")]
                    if passed:
                        best = max(passed, key=lambda x: int(x))
                if best:
                    rejectto = best
        # 删除"chunk 开头到第一个 procuserofwfprocs/useropinionofwfprocs 之间"的旧顶层 opinion/rejectto
        for _tag in ("procuserofwfprocs", "useropinionofwfprocs", "wfuserrangeofwfprocs"):
            _idx = chunk.find("<" + _tag + ">")
            if _idx >= 0:
                _head = chunk[:_idx]
                _head = _re.sub(r"<opinion>[^<]*</opinion>", "", _head, count=1)
                _head = _re.sub(r"<rejectto>[^<]*</rejectto>", "", _head, count=1)
                chunk = _head + chunk[_idx:]
                break
        chunk = f"<opinion>{opinion}</opinion><rejectto>{rejectto}</rejectto>" + chunk
        # 5) 顶层 <stat> 强制设为 4（已完成）：
        #    原生客户端 reject body（wfid=1329815, procid=3）顶层 stat=4；
        #    select 返回进行中节点 stat=5。服务端可能依赖 stat=4 识别"驳回"，
        #    stat=5 会被当"提交"处理（1329808 事故正是 stat=5 → 流程被直接关闭）。
        chunk = _re.sub(r"(<stat>)[^<]*(</stat>)", r"\g<1>4\g<2>", chunk, count=1)
        # 5b) 顶层 flag 强制设为 0（原生 reject body 实测 flag=0；
        #     select 返回值不确定，显式置 0 避免被服务端当 submit 语义处理）
        if _re.search(r"<flag>", chunk):
            chunk = _re.sub(r"(<flag>)[^<]*(</flag>)", r"\g<1>0\g<2>", chunk, count=1)
        else:
            chunk = chunk.replace("<width>", "<flag>0</flag><width>", 1)
        # 5) 修正当前用户 procuser issigned=1 / freq=1
        chunk = _re.sub(
            rf"(<procuserofwfproc>(?:(?!</procuserofwfproc>).)*?<userid>{_re.escape(user)}</userid>(?:(?!</procuserofwfproc>).)*?)(<issigned>)[^<]*(</issigned>)",
            lambda mm: mm.group(1) + mm.group(2) + "1" + mm.group(3), chunk, flags=_re.S, count=1)
        chunk = _re.sub(
            rf"(<procuserofwfproc>(?:(?!</procuserofwfproc>).)*?<userid>{_re.escape(user)}</userid>(?:(?!</procuserofwfproc>).)*?)(<freq>)[^<]*(</freq>)",
            lambda mm: mm.group(1) + mm.group(2) + "1" + mm.group(3), chunk, flags=_re.S, count=1)
        # 6) nodepath：沿箭头拓扑从 0 BFS 走到当前节点（1330666 实证 preproc 链会走错分支：
        #    品质会签(2) 的 preproc 链给 0-1-2，图上真实路径是 0-7-2）；无箭头数据退回 preproc 链
        path = []
        if conds:
            _fwd = {}
            for f, t in conds:
                _fwd.setdefault(f, []).append(t)
            _prev = {"0": None}
            _queue = ["0"]
            while _queue and procid_s not in _prev:
                c = _queue.pop(0)
                for t in _fwd.get(c, []):
                    if t in _prev:
                        continue
                    _prev[t] = c
                    _queue.append(t)
            if procid_s in _prev:
                n2 = procid_s
                while n2 is not None:
                    path.insert(0, n2)
                    n2 = _prev[n2]
        if not path:
            node, guard = procid_s, 0
            while node in procs and node not in path and node != "-1" and guard < 60:
                path.insert(0, node)
                node = procs[node]
                guard += 1
            if node not in path and node != "-1":
                path.insert(0, node)
        nodepath = "-".join(path)
        chunk += (f"<wfnodepathofwfprocs><wfnodepathofwfproc><wfid>0</wfid><procid>0</procid>"
                  f"<nodepath>{nodepath}</nodepath><unsubmitnodes></unsubmitnodes>"
                  f"<rejectnodes></rejectnodes></wfnodepathofwfproc></wfnodepathofwfprocs>")
        # 调试落盘：把构造 body 写盘，方便与原生客户端 hit2 模板对比
        try:
            import pathlib
            _dbg = pathlib.Path(__file__).resolve().parents[1] / "docs" / "captured" / "wf_reject_capture" / "last_reject_body.xml"
            _dbg.parent.mkdir(parents=True, exist_ok=True)
            _dbg.write_text(chunk, encoding="utf-8")
            _dbg2 = pathlib.Path(__file__).resolve().parents[1] / "docs" / "captured" / "wf_reject_capture" / "last_reject_select.xml"
            _dbg2.write_text(sel, encoding="utf-8")
        except Exception:
            pass
        resp = await self.call("wfman.jsp", "cpcwfproc", "reject", chunk)
        try:
            _dbg3 = pathlib.Path(__file__).resolve().parents[1] / "docs" / "captured" / "wf_reject_capture" / "last_reject_resp.xml"
            _dbg3.write_text(resp, encoding="utf-8")
        except Exception:
            pass
        return resp

    # ---- BOM ----
    async def bom_selectref(self, asmid: str = "0", invorgid: str = "104",
                            itemcode: str = "", itemname: str = "", rev: str = "") -> str:
        """BOM 展开。服务端校验严格（精简字段 41124），必须用完整抓包模板。"""
        data = _fill_template(BOM_SELECTREF, {
            "asmid": asmid, "invorgid": invorgid,
            "itemcode": itemcode, "itemname": itemname, "rev": rev,
        })
        return await self.call("bomman.jsp", "cpcbom", "selectref", data)

    async def item_getreference(self, asmcode: str, drawid: str = "", searchflag: str = "1",
                                filter_disabled: bool = True, invorgid: str = "104") -> str:
        """零部件引用查询（2026-09-09 抓包实证，4 样本）：
        bomman.jsp?classid=cpcitemsearch&action=getreference
        searchflag: 1=直接上层  2=引用产品
        filter_disabled: True→actived=-1（仅有效引用）  False→actived=1（含禁用）
        needsuperflag 原样照抄：SF1→2、SF2→0。
        响应行 = <itemsearchofitemsearch>（30-34 字段，行 actived: 2=有效 1=禁用）。"""
        data = (ITEM_GETREFERENCE
                .replace("__ASMCODE__", asmcode)
                .replace("__DRAWID__", drawid)
                .replace("__SEARCHFLAG__", searchflag)
                .replace("__NEEDSUPERFLAG__", "2" if searchflag == "1" else "0")
                .replace("__ACTIVED__", "-1" if filter_disabled else "1")
                .replace("__INVORGID__", invorgid))
        return await self.call("bomman.jsp", "cpcitemsearch", "getreference", data)

    # ---- 产品工作区 / 文件夹树（vaultman.jsp，复刻客户端「产品工作区」） ----
    async def workspace_selectall(self, wstype: str = "4", modid: str = "12", userid: str = "") -> str:
        """工作区列表。2026-08-25 抓包实证（myfiles_capture）：
        我的文件模块 modid=12、wstype=4（个人工作区）、userid 回填当前用户；
        响应 <workspaces><workspace>（wsid/wsname/wstype），含个人工作区 + 回收站(-99)。"""
        data = (f"<wsid>0</wsid><wstag>0</wstag><wsname></wsname><wstype>{wstype}</wstype>"
                f"<modid>{modid}</modid><userid>{userid}</userid><note></note>"
                f"<entid>0</entid><objtype>0</objtype><maxsearchrltcmt>0</maxsearchrltcmt>"
                f"<sqlwhere></sqlwhere><wsright></wsright><excluderight>0</excluderight>"
                f"<rightofobjs></rightofobjs><maxspace>0</maxspace><usespace>0</usespace>")
        return await self.call("vaultman.jsp", "cpcworkspace", "selectall", data)

    async def workspace_selectref(self, wsid: str = "-14", wsname: str = "产品工作区",
                                  wstype: str = "1") -> str:
        """工作区根。2026-08-24/08-25 抓包实证（产品=-14/wstype=1；文档=-4/wstype=1；我的文件=个人 wsid/wstype=4）：
        POST /jsp/vaultman.jsp?classid=cpcworkspace&action=selectref
        请求体含 <wsid>X</wsid><wsname>...</wsname><wstype>1</wstype>
        响应 <fdrs> 下为工作区根下的直接子文件夹（我的文件场景含 <shortcuts> 共享快捷方式）。"""
        data = (f"<wsid>{wsid}</wsid><wstag>{wsid}</wstag><wsname>{wsname}</wsname>"
                f"<wstype>{wstype}</wstype><modid>0</modid><userid></userid><note></note>"
                f"<entid>1</entid><objtype>0</objtype><maxsearchrltcmt>0</maxsearchrltcmt>"
                f"<sqlwhere></sqlwhere>")
        return await self.call("vaultman.jsp", "cpcworkspace", "selectref", data)

    async def fdr_selectref(self, fdrid: str, fdrname: str = "",
                            idpath: str = "", typepath: str = "",
                            parentid: str = "", parenttype: str = "",
                            objaccess: str = "", objaccesstype: str = "") -> str:
        """展开文件夹。2026-08-24 抓包实证：
        POST /jsp/vaultman.jsp?classid=cpcfdr&action=selectref
        请求体含 <fdrid>X</fdrid><fdrname>...</fdrname> 以及定位上下文
        <idpath>-14\\{fdrid}\\</idpath><typepath>1\\2\\</typepath>
        <parentid>-14</parentid><parenttype>1</parenttype>。
        实测：idpath/typepath/parentid 缺失时服务端返回空的 <fdrs/>，必须带上。
        响应：<fdrs> 子文件夹列表（attr=1）+ <boms><bom> 物料列表（叶子层 fdrs 为空）。"""
        if not parentid:
            parentid = "-14" if not idpath else ""
        oa = objaccess or "2"
        oat = objaccesstype or "6"
        data = (f"<fdrid>{fdrid}</fdrid><fdrname>{fdrname}</fdrname><rev>0</rev>"
                f"<revlabel>0</revlabel><clas>0</clas><stat>0</stat><flag>2</flag>"
                f"<locked>0</locked><inspect>0</inspect><issecret>1</issecret>"
                f"<creator></creator><createtime></createtime><updator></updator>"
                f"<updatetime></updatetime><updatereason></updatereason><publishor></publishor>"
                f"<publishtime></publishtime><publishstatus>0</publishstatus><publishstage>0</publishstage>"
                f"<actived>2</actived><note></note><objaccess>{oa}</objaccess><objaccesstype>{oat}</objaccesstype>"
                f"<wsright>40620002400000000040000000400000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000</wsright>"
                f"<parentid>{parentid}</parentid><parenttype>{parenttype or '1'}</parenttype>"
                f"<namepath></namepath><excluderight>1</excluderight><isupdateright>0</isupdateright>"
                f"<attribute>1</attribute><sourceid>0</sourceid>"
                f"<idpath>{idpath}</idpath><typepath>{typepath}</typepath>"
                f"<sysbuiltinobj>0</sysbuiltinobj><subcount>0</subcount><getallwftemp>0</getallwftemp>"
                f"<projname></projname><objname></objname><fullpath></fullpath>"
                f"<teamid></teamid><manageid></manageid><teamname></teamname><searchreftype>0</searchreftype>"
                f"<usedspace>0</usedspace><maxspace>0</maxspace><doccid>0</doccid><doccname></doccname>"
                f"<objrights></objrights><objdefprops></objdefprops><objcrosss></objcrosss>"
                f"<docs></docs>")
        return await self.call("vaultman.jsp", "cpcfdr", "selectref", data)

    async def shortcut_selectref(self, refid: str, scname: str = "",
                                 idpath: str = "", typepath: str = "",
                                 sourceidpath: str = "", sourcetypepath: str = "",
                                 reftype: str = "-2", objaccess: str = "",
                                 objaccesstype: str = "", objaccesspass: str = "",
                                 wsright: str = "", parentid: str = "", parenttype: str = "1") -> str:
        """展开共享快捷方式（别人共享给我的目录）。

        2026-09-02 抓包实证（shared_with_me_v2 REQ3，37 标签逐字）：
        POST /jsp/vaultman.jsp?classid=cpcshortcut&action=selectref
        请求体 = workspace 根响应 <shortcut> 行 10 字段原样回传 + data 层 <wsright> 原样回传，
        其余默认（scid=0/reftype=-2 固定、parentid=idpath 第一段=父 wsid、parenttype=1、excluderight=1）。
        勿自拼缺字段：objaccess/objaccesstype/sourceidpath/sourcetypepath 任一缺失服务端即无法定位。
        响应：<fdrs/>（子目录非空形态未实证）+ <docs>（doc 行与 cpcfdr.selectref 同构，可复用 doc 解析）。
        """
        if not parentid and idpath:
            parentid = idpath.rstrip("\\").split("\\")[0] if idpath.rstrip("\\") else ""
        data = (f"<scid>0</scid><scname>{scname}</scname><refid>{refid}</refid>"
                f"<reftype>{reftype or '-2'}</reftype><refaddr></refaddr>"
                f"<wfflag>0</wfflag><wfid>0</wfid><procid>0</procid>"
                f"<creator></creator><createtime></createtime><updator></updator>"
                f"<updatetime></updatetime><note></note><sourcepathname></sourcepathname>"
                f"<sourceidpath>{sourceidpath}</sourceidpath>"
                f"<sourcetypepath>{sourcetypepath}</sourcetypepath>"
                f"<sourcewsright></sourcewsright><sourcecode></sourcecode>"
                f"<sourcemodid>0</sourcemodid><refrev>0</refrev>"
                f"<objaccess>{objaccess}</objaccess><objaccesstype>{objaccesstype}</objaccesstype>"
                f"<objaccesspass>{objaccesspass}</objaccesspass>"
                f"<wsright>{wsright}</wsright>"
                f"<parentid>{parentid}</parentid><parenttype>{parenttype or '1'}</parenttype>"
                f"<idpath>{idpath}</idpath><typepath>{typepath}</typepath><namepath></namepath>"
                f"<excluderight>1</excluderight><isupdateright>0</isupdateright>"
                f"<refstat>0</refstat><ismajor>0</ismajor>"
                f"<suppname></suppname><suppid>0</suppid><objrights></objrights>")
        return await self.call("vaultman.jsp", "cpcshortcut", "selectref", data)

    # ---- 文档搜索（publicman.jsp cpcexplorer.search，2026-08-25 抓包 doc_search_capture 实证） ----
    async def explorer_search(self, keyword: str = "", parentid: str = "", parenttype: str = "",
                              wsid: str = "-4", datebegin: str = "", dateend: str = "",
                              maxsearchrltcmt: int = 500) -> str:
        """文件夹右键「搜索」：按 objname 前缀模糊搜当前文件夹及其子文件夹。

        请求体（抓包实证）：objname=关键字（响应回显 %关键字=前缀模糊）；searchtype=6；
        parentid=当前文件夹完整路径（如 -4\\115179\\...\\406246）；includechildobj=2 含子文件夹；
        datecreated=1 + datebeween=2 + datebegin/dateend 时间范围（客户端默认近一月）；
        响应 <docofexplores><docofexplore>（含 namepath 完整路径）。
        """
        if not datebegin or not dateend:
            # 默认近一月（对齐客户端 datebeforemonth=1）
            import datetime
            today = datetime.date.today()
            dateend = today.isoformat()
            datebegin = (today - datetime.timedelta(days=30)).isoformat()
        wsright = ("6000000060000000000000000060000000000000000000000000000000000000000000000"
                   "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
                   "0000000000000000000000000000000000000000000000000000000000")
        data = (f"<objid></objid><objaccess>891</objaccess><objname>{keyword}</objname><objcode></objcode>"
                f"<objtype>0</objtype><keyword></keyword><drawid></drawid><drawname></drawname><spec></spec>"
                f"<modelid></modelid><includetext></includetext><includedate>1</includedate>"
                f"<datecreated>1</datecreated><datebeforemonth>1</datebeforemonth><datemonth>0</datemonth>"
                f"<datebeforeday>1</datebeforeday><dateday>0</dateday><datebeween>2</datebeween>"
                f"<datebegin>{datebegin}</datebegin><dateend>{dateend}</dateend>"
                f"<searchtype>6</searchtype><includesize>1</includesize><sizecompare>1</sizecompare><size>0</size>"
                f"<includeother>2</includeother><includechildobj>2</includechildobj><path></path>"
                f"<wsright>{wsright}</wsright><wsid>{wsid}</wsid><updatetime></updatetime>"
                f"<onlyroot>0</onlyroot><stat>0</stat><searchflag>0</searchflag>"
                f"<maxsearchrltcmt>{maxsearchrltcmt}</maxsearchrltcmt><idxtext></idxtext>"
                f"<parentid>{parentid}</parentid><parenttype>{parenttype}</parenttype>"
                f"<userid></userid><email></email><actived>0</actived><sqlwhere></sqlwhere>"
                f"<namecase>2</namecase><codecase>2</codecase><drawidcase>2</drawidcase>")
        return await self.call("publicman.jsp", "cpcexplorer", "search", data)

    # ---- 文件下载（fileman.jsp，复刻客户端附件「打开」） ----
    async def file_getdownstatus(self, docid: str, rev: str = "1") -> str:
        """fileman.jsp getdownstatus：传入 docid，返回下载坐标。

        响应明文 XML：<url>(文件服务器，如 http://10.240.28.24:8011/) /
        <tmpfilename>(服务器侧缓存路径) / <docname> / <docid> / <rev> / <filesize>。
        请求体为客户端 ZIP 通道（0x02+ZIP{UTF-16 XML}）。
        """
        data = f"<docid>{docid}</docid><rev>{rev}</rev>"
        return await self.call("fileman.jsp", "file", "getdownstatus", data)

    # ---- 文件检出/检入（docman.jsp，复刻客户端「打开即锁定 / 检入变更」） ----
    async def doc_select(self, docid: str, rev: str = "1") -> str:
        """cpcdoc.select：获取文档完整信息（流程中文档检出的前置步骤）。"""
        data = f"<docid>{docid}</docid><rev>{rev}</rev>"
        return await self.call("docman.jsp", "cpcdoc", "select", data)

    async def doc_checkout(self, docid: str, rev: str = "1") -> tuple[str, str]:
        """cpcdoc.checkout：检出（锁定）文档。

        抓包实证（2026-08-18）：流程文档不能只用 <docid><rev>，必须先 cpcdoc.select
        取得完整 XML（含 wfflag/wfid/flag/locked 等），再用该 XML 调 checkout，
        否则服务端返回 40212「在走工作流，不能检出」。

        返回：(checkout 响应 XML, checkout 请求体 XML)。调用方应把请求体作为检入模板缓存。
        """
        select_xml = await self.doc_select(docid, rev)
        # 提取 <response><data> 内部字段作为 checkout 请求体内部字段
        # build_body 会自行包装成 <Request><Data>...</Data></Request>
        inner = _extract_data_inner(select_xml)
        if not inner:
            raise RuntimeError(f"cpcdoc.select 未返回有效 data：{select_xml[:300]}")
        # 服务端 XML 解析器对标签间空白敏感：必须压缩成无空白的紧凑 XML，
        # 否则 <Data> 后的换行/缩进会导致 docid 被读成 0（30049）。
        inner_compact = re.sub(r">\s+<", "><", inner.strip())
        resp = await self.call("docman.jsp", "cpcdoc", "checkout", inner_compact)
        return resp, inner_compact

    async def doc_undocheckout(self, docid: str, rev: str = "1") -> str:
        """cpcdoc.undocheckout：撤销检出（解锁），lockuser 清空，不生成新版本。"""
        data = f"<docid>{docid}</docid><rev>{rev}</rev>"
        return await self.call("docman.jsp", "cpcdoc", "undocheckout", data)

    async def doc_checkin(self, docid: str, data_xml: str) -> str:
        """cpcdoc.checkin：检入（上传新版本）。data_xml 为完整文档 XML（基于 checkout 响应修改上传坐标）。"""
        return await self.call("docman.jsp", "cpcdoc", "checkin", data_xml)

    async def file_download(self, file_server_url: str, tmpfilename: str, loginguid: str,
                            packagesize: int = 52428800, filepos: int = 0) -> bytes:
        """从文件服务器下载真实文件字节（如 http://10.240.28.24:8011/）。

        抓包实证（2026-08-18）：
          GET /jsp/fileman.jsp?loginguid=...&classid=file&action=download
              &tmpfilename=<tmpfilename>&filepos=0&packagesize=<期望字节数>
          User-Agent: Mozilla/3.0 (compatible;Super Indy Library)
          cpccookie: sessiontimestamp=<毫秒>
        响应体 = 0x03 0x01 0x00 + 存储字节 + 0x03（1 字节终止符）。

        2026-08-20 修正：packagesize 必须覆盖整个文件，否则 >1MB 文件只返回首包，
        导致 PDF/AI 等大文件损坏。服务端把 packagesize 视为压缩字节上限，传 filesize
        或一个足够大的默认值可一次性取回完整文件。

        2026-09-03 实测补充（filepos 随机读 + 存储口径）：
          - filepos 为【存储文件内偏移】(0-based)，服务端支持随机读，
            实测 6474463/6419508 两份文档 filepos=(filesize-40) 与全量下载逐字节一致。
          - 响应恒定多带 1 字节 0x03 终止符：请求 N 字节 → 响应体 3+N+1。
          - **存储文件不含上传时的 0x01 方法字节**（服务端存盘时已剥离），
            即 filesize == len(file_z + meta_z + tail)，download payload[0] 直接是 zlib 头 78 9c。
        """
        base = file_server_url.rstrip("/")
        url = f"{base}/jsp/fileman.jsp"
        params = {
            "loginguid": loginguid,
            "classid": "file",
            "action": "download",
            "tmpfilename": tmpfilename,
            "filepos": str(filepos),
            "packagesize": str(packagesize),
        }
        headers = {
            "User-Agent": "Mozilla/3.0 (compatible;Super Indy Library)",
            "Content-Type": "image/jpeg",
            "cpccookie": f"sessiontimestamp={int(time.time() * 1000)}",
        }
        resp = await self.session.get(url, params=params, headers=headers)
        log.info("file_download status=%s raw_len=%s packagesize=%s", resp.status_code, len(resp.content), packagesize)
        if resp.status_code != 200:
            raise RuntimeError(f"download HTTP {resp.status_code}: {resp.content[:200]!r}")
        return resp.content

    # ---- 文件上传（检入用，fileman.jsp action=upload，直连文件服务器） ----
    async def file_getupstatus(self, docid: str, locid: str = "7", guid: str = None) -> str:
        """fileman.jsp getupstatus：取上传坐标（文件服务器 url + 服务器侧 tmpfilename GUID）。

        抓包实证（2026-08-18）：请求体 ZIP 通道
        <Request><Data><wsid>0</wsid><docid>..</docid><locid>7</locid>
        <tmpfilename>{GUID}</tmpfilename><filetag></filetag></Data></Request>
        响应含 <url>http://10.240.28.24:8011/</url> 与 <tmpfilename>d:/vault_prd/.../{GUID}</tmpfilename>。
        GUID 由调用方生成（客户端自生成）。
        """
        if guid is None:
            guid = "{" + str(uuid.uuid4()).upper() + "}"
        data = (f"<wsid>0</wsid><docid>{docid}</docid><locid>{locid}</locid>"
                f"<tmpfilename>{guid}</tmpfilename><filetag></filetag>")
        return await self.call("fileman.jsp", "file", "getupstatus", data)

    async def file_upload(self, file_server_url: str, server_tmpfilename: str, raw: bytes,
                          login_sessiontimestamp: str = None, filename: str = "") -> tuple:
        """直连文件服务器上传文件字节（检入用）。

        抓包实证（2026-08-18）：
          POST http://10.240.28.24:8011/jsp/fileman.jsp
              ?loginguid=...&classid=file&action=upload
              &tmpfilename=d:/vault_prd/.../{GUID}&packagesize=<体长>&filepos=0&crc32=<有符号>
          Body = 0x01 + zlib(文件) + zlib(788B元数据) + 36B尾   ← 2026-09-03 修正

        2026-08-18 修正（50003 根因）：crc32 必须是【压缩后字节】的 CRC32 有符号值，
          不是原始文件字节的 CRC32！计算错误会被文件服务器拒绝(50003 null)。
        2026-09-03 重大修正（原版客户端打开报「解压失败」根因）：
          原先只发 0x01 + zlib(文件)，缺元数据块 + 尾段。服务器照存，但原版客户端
          下载后解完第一段即去读元数据段 → EOF → 抛「解压失败」，temp 不落盘。
          现改为 build_cpc() 构造完整体；packagesize = 0x01 之后全部字节数，
          crc32 = 0x01 之后全部字节的 CRC32（3 份抓包 5/5 校验通过）。

        返回 (响应内容, 0x01 之后的负载字节)，供 checkin 复用同一 crc32/packagesize。
        """
        payload = build_cpc(raw, filename)  # 含 0x01 前缀
        comp = payload[1:]                  # 校验口径 = 0x01 之后全部
        crc = zlib.crc32(comp) & 0xffffffff
        crc_signed = crc if crc < 2**31 else crc - 2**32
        base = file_server_url.rstrip("/")
        params = {
            "loginguid": self.login_uid,
            "classid": "file",
            "action": "upload",
            "tmpfilename": server_tmpfilename,
            "packagesize": str(len(comp)),
            "filepos": "0",
            "crc32": str(crc_signed),
        }
        headers = {
            "User-Agent": "Mozilla/3.0 (compatible;Super Indy Library)",
            "Content-Type": "image/jpeg",
            # 优先用登录时刻 sessiontimestamp（真实客户端行为）；无则用当前时间
            "cpccookie": f"sessiontimestamp={login_sessiontimestamp or getattr(self, '_login_timestamp', None) or int(time.time() * 1000)}",
        }
        url = f"{base}/jsp/fileman.jsp"
        resp = await self.session.post(url, params=params, content=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"upload HTTP {resp.status_code}: {resp.content[:200]!r}")
        return resp.content, comp

    # ---- 我的文件：导入 / 重命名 / 复制 / 剪切 / 删除（2026-09-08 抓包实证） ----
    async def doc_import(self, *, wsid: str, parentid: str, parenttype: str,
                         filename: str, raw: bytes, client_path: str = "") -> dict:
        """把本地文件导入到「我的文件」某文件夹，返回新文档信息 {docid, docname, idpath, ...}。

        三步（docs/captured/myfiles_ops_v1/PROTOCOL.md）：
          1. fileman file getupstatus：<wsid>{工作区}</wsid><docid>0</docid><locid>-1</locid><tmpfilename>{GUID}</tmpfilename>
             → 回 <locid>7</locid> + <url>http://10.240.28.24:8011/</url> + <tmpfilename>d:/vault_prd/…/{GUID}</tmpfilename>
          2. 直连文件服务器 upload：body = 0x01 + build_cpc(raw, filename)，
             packagesize = 0x01 之后字节数，crc32 = 0x01 之后字节的 CRC32（有符号）
          3. docman cpcdoc insert：模板 + 17 个文件字段（docid=0）→ 响应回新 docid
        """
        import uuid as _uuid
        guid = "{" + str(_uuid.uuid4()).upper() + "}"
        st_xml = await self.call("fileman.jsp", "file", "getupstatus",
                                 f"<wsid>{wsid}</wsid><docid>0</docid><locid>-1</locid>"
                                 f"<tmpfilename>{guid}</tmpfilename><filetag></filetag>")
        st = _xml_fields(st_xml)
        url = (st.get("url") or "").strip()
        server_tmp = (st.get("tmpfilename") or "").strip()
        locid = (st.get("locid") or "7").strip() or "7"
        if not url or not server_tmp:
            raise RuntimeError(f"getupstatus 未返回上传坐标：{st_xml[:300]}")

        _, comp = await self.file_upload(url, server_tmp, raw, filename=filename)
        zipsize = str(len(comp))
        oldsize = str(len(raw))
        crc_file = zlib.crc32(raw) & 0xffffffff
        oldcrc32 = str(crc_file if crc_file < 2**31 else crc_file - 2**32)
        crc_comp = zlib.crc32(comp) & 0xffffffff
        crc32 = str(crc_comp if crc_comp < 2**31 else crc_comp - 2**32)
        up_tmp = f"C:\\Users\\{filename}.zlb"  # 本地临时 .zlb 路径（服务端仅记录，不校验）

        row = build_doc_row({
            "docid": "0", "docname": filename, "rev": "0", "appid": "13",
            "parentid": parentid, "parenttype": parenttype,
            "revlabel": "A.1", "doctype": "1", "locid": locid,
            "exptime": "9999-12-31", "pagecount": "1", "pagenum": "1",
            "weight": "1", "isziped": "2", "zipsize": zipsize, "oldsize": oldsize,
            "crc32": crc32, "oldcrc32": oldcrc32,
            "clientfilename": client_path or filename,
            "tmpfilename": server_tmp, "uploadfilename": up_tmp,
            "isattach": "1", "sysbuiltinobj": "1",
        })
        ins_xml = await self.call("docman.jsp", "cpcdoc", "insert", row)
        f = _xml_fields(ins_xml)
        return {"xml": ins_xml, "docid": f.get("docid", ""), "docname": f.get("docname", ""),
                "revlabel": f.get("revlabel", ""), "idpath": f.get("idpath", ""),
                "parentid": f.get("parentid", ""), "server_tmpfilename": server_tmp}

    async def doc_rename(self, docid: str, new_name: str, rev: str = "1") -> dict:
        """重命名文档：请求体 = cpcdoc select 回显整行 + 改 docname（旧名由服务端回填 olddocname）。

        实证 REQ7 vs REQ9：整行只差 flag/updatetime/olddocname 三个非业务字段 → 回显照抄即可。
        """
        sel_xml = await self.doc_select(docid, rev)
        inner = _extract_data_inner(sel_xml)
        if not inner:
            raise RuntimeError(f"cpcdoc.select 未返回有效 data：{sel_xml[:300]}")
        compact = re.sub(r">\s+<", "><", inner.strip())
        row = re.sub(r"<docname>[^<]*</docname>", f"<docname>{new_name}</docname>", compact, count=1)
        resp = await self.call("docman.jsp", "cpcdoc", "rename", row)
        f = _xml_fields(resp)
        return {"xml": resp, "docid": f.get("docid", ""), "docname": f.get("docname", ""),
                "olddocname": f.get("olddocname", "")}

    async def doc_paste(self, docid: str, dest_parentid: str, dest_parenttype: str,
                        rev: str = "1", move: bool = False, docname: str = "") -> dict:
        """复制粘贴(paste) / 剪切移动(cut)。

        实证 REQ10/REQ13：两者请求体**逐字段同构**，都是「轻量行」——只有
        docid/docname/rev/appid=13/parentid/parenttype 有值，其余全是模板默认(0/空)。
        parentid/parenttype = **目标文件夹**的 idpath/typepath。
        paste → 服务端分配**新 docid**（副本）；cut → **docid 不变**且响应 act=6（移动）。
        """
        if not docname:
            f = _xml_fields(await self.doc_select(docid, rev))
            docname = f.get("docname", "")
            rev = f.get("rev", rev) or rev
        row = build_doc_row({
            "docid": docid, "docname": docname, "rev": rev, "appid": "13",
            "parentid": dest_parentid, "parenttype": dest_parenttype,
        })
        resp = await self.call("docman.jsp", "cpcdoc", "cut" if move else "paste", row)
        f = _xml_fields(resp)
        return {"xml": resp, "docid": f.get("docid", ""), "docname": f.get("docname", ""),
                "act": f.get("act", ""), "moved": bool(move)}

    async def doc_delete(self, row_fields: dict, parentid: str = "", parenttype: str = "",
                         wsright: str = "") -> str:
        """删除文档。

        实证 REQ15：请求体 = **文件夹列表里的 <doc> 子表行**（42 字段）铺进 190 字段模板，
        再补 parentid/parenttype（所属文件夹）与 wsright（父文件夹行的 wsright）；
        列表行与请求行实测只有 objaccesstype 不同（请求里为 0，已强制置 0）。
        """
        ov = dict(row_fields or {})
        ov["objaccesstype"] = "0"
        if parentid:
            ov["parentid"] = parentid
        if parenttype:
            ov["parenttype"] = parenttype
        if wsright:
            ov["wsright"] = wsright
        row = build_doc_row(ov)
        return await self.call("docman.jsp", "cpcdoc", "delete", row)

    # ---------- 文件夹（cpcfdr）写操作（2026-09-08 folder_ops_v1 抓包实证） ----------
    # 协议：vaultman.jsp classid=cpcfdr，写操作共用 51 字段固定顺序模板（app/doc_templates.py）

    async def fdr_create(self, name: str, parentid: str, parenttype: str,
                         wsright: str = "", objaccess: str = "127") -> dict:
        """新建文件夹（cpcfdr insert，REQ2 实证）：
        模板 + fdrname/issecret=1/actived=2/objaccess=127/wsright/parentid/parenttype/
        isupdateright=1/attribute=1/sysbuiltinobj=1（fdrid=0）。响应回新 fdrid + 全行。"""
        row = build_fdr_row({
            "fdrid": "0", "fdrname": name,
            "issecret": "1", "actived": "2", "objaccess": objaccess,
            "wsright": wsright, "parentid": parentid, "parenttype": parenttype,
            "isupdateright": "1", "attribute": "1", "sysbuiltinobj": "1",
        })
        resp = await self.call("vaultman.jsp", "cpcfdr", "insert", row)
        f = _xml_fields(resp)
        return {"xml": resp, "fdrid": f.get("fdrid", ""), "fdrname": f.get("fdrname", ""),
                "idpath": f.get("idpath", ""), "typepath": f.get("typepath", "")}

    async def fdr_rename(self, row_fields: dict, new_name: str) -> dict:
        """重命名文件夹（REQ3 实证）：请求 = cpcfdr select 回显整行 + 改 fdrname。
        row_fields 为服务端从父文件夹列表取出的 <fdr> 行（13 字段）——
        先用其发一次 cpcfdr select 换取 37 字段完整行（含 stat/updator/namepath/sysbuiltinobj），
        再改 fdrname 发 rename。"""
        base = {k: v for k, v in (row_fields or {}).items()}
        sel_req = build_fdr_row(base)
        sel_xml = await self.call("vaultman.jsp", "cpcfdr", "select", sel_req)
        full = _xml_fields(sel_xml)
        if not full.get("fdrid"):
            raise RuntimeError(f"cpcfdr.select 未返回有效行：{sel_xml[:200]}")
        # select 响应的 parenttype 可能被服务端按请求行回显污染（空 parenttype 时回 '1\2\2'），
        # 以调用方推导的权威值覆写（41124 父对象路径错误的根因）
        for k in ("parentid", "parenttype"):
            if base.get(k):
                full[k] = base[k]
        full["fdrname"] = new_name
        rn_req = build_fdr_row(full)
        resp = await self.call("vaultman.jsp", "cpcfdr", "rename", rn_req)
        f = _xml_fields(resp)
        return {"xml": resp, "fdrid": f.get("fdrid", ""), "fdrname": f.get("fdrname", ""),
                "olddocname": f.get("olddocname", "")}

    async def fdr_copy(self, fdrid: str, fdrname: str, dest_parentid: str,
                       dest_parenttype: str, wsright: str = "") -> dict:
        """复制文件夹（cpcfdr paste，REQ5 实证）：轻量行 fdrid/fdrname/wsright/parentid/parenttype。
        ⚠ fdrname 由客户端预生成（同父粘贴时原版客户端用「复件N 名」），调用方负责命名。"""
        row = build_fdr_row({"fdrid": fdrid, "fdrname": fdrname, "wsright": wsright,
                             "parentid": dest_parentid, "parenttype": dest_parenttype})
        resp = await self.call("vaultman.jsp", "cpcfdr", "paste", row)
        f = _xml_fields(resp)
        return {"xml": resp, "fdrid": f.get("fdrid", ""), "fdrname": f.get("fdrname", ""),
                "idpath": f.get("idpath", "")}

    async def fdr_move(self, fdrid: str, fdrname: str, dest_parentid: str,
                       dest_parenttype: str, wsright: str = "") -> dict:
        """剪切/移动文件夹（cpcfdr cut，REQ13 实证）：**单发直达**——parentid 直接填
        目标文件夹路径，无需再 paste。响应回显移动后的行（namepath=新路径）。"""
        row = build_fdr_row({"fdrid": fdrid, "fdrname": fdrname, "wsright": wsright,
                             "parentid": dest_parentid, "parenttype": dest_parenttype})
        resp = await self.call("vaultman.jsp", "cpcfdr", "cut", row)
        f = _xml_fields(resp)
        return {"xml": resp, "fdrid": f.get("fdrid", ""), "fdrname": f.get("fdrname", ""),
                "namepath": f.get("namepath", ""), "moved": True}

    async def fdr_delete(self, row_fields: dict, parentid: str = "", parenttype: str = "",
                         wsright: str = "") -> str:
        """删除文件夹（REQ16 实证）：请求 = 列表 <fdr> 行 + parentid/parenttype（所属文件夹）
        + wsright。与文档删除同构。"""
        ov = dict(row_fields or {})
        if parentid:
            ov["parentid"] = parentid
        if parenttype:
            ov["parenttype"] = parenttype
        if wsright:
            ov["wsright"] = wsright
        return await self.call("vaultman.jsp", "cpcfdr", "delete", build_fdr_row(ov))

    async def close(self):
        await self.session.aclose()
        if self._web:
            await self._web.close()


CPC_META_SIZE = 788  # 原版 .cpc 元数据块固定长度（3 个抓包样本一致）


def build_cpc(raw: bytes, filename: str = "", timestamp: str = None) -> bytes:
    """构造原版客户端 .cpc 上传体（2026-09-03 抓包钉死，3 样本全字段对上）。

    结构  0x01 + zlib(文件) + zlib(788B元数据块) + 36B尾
    元数据块(788B, 其余为零填充)：
        [0]     文件名 UTF-8 字节数 + 1
        [1]     0x7F（常量）
        [2:512] 文件名 UTF-8（名字区，最长 254B = [0] 单字节上限）
                ⚠ 09-09 修正：09-03 因 3 个样本文件名都短（剩余字节恰为 0），
                  误判名字区只有 [2:114] 112B 并在此截断 → 长中文名被切在
                  汉字中间、扩展名丢失，原版客户端打开报「解压失败」
                  （wf 1331113 / docid 6476990 实证）。原版对照样本 docid 6476992：
                  名字 125B 连续写入 [2:127]，[127:512] 全零 → 名字区实为 [2:512]。
        [512]   0x13（常量）
        [513:533] "YYYY-MM-DD HH:MM:SS" + NUL
        [768:772] 原始文件大小   LE u32
        [772:776] zlib(文件)长度 LE u32
        [776:780] 0
        [780:784] crc32(原始文件) LE u32
        [784:788] 00 02 00 00（常量）
    36B尾：
        [0:4]   crc32(zlib(文件) + zlib(元数据)) LE u32
        [4:8]   1  [8:12] 1  [12:16] zlib(元数据)长度  [16:20] zlib(文件)长度  [20:24] 1  [24:36] 零

    ⚠ 只发 0x01+zlib(文件) 时服务器照样收存，但原版客户端下载后解析元数据段 EOF
      → 报「解压失败」，temp 文件不落盘（docid 6419508 实测）。故必须构造完整体。
    """
    file_z = zlib.compress(raw)
    meta = bytearray(CPC_META_SIZE)
    # 名字区实为 [2:512]（09-09 原版样本实证），[0] 为单字节长度+1 → 最长 254B。
    # 超长时按 UTF-8 字符边界截断（丢弃残缺尾字符，绝不切在汉字中间）。
    name = (filename or "").encode("utf-8", "ignore")[:254]
    if len(name) == 254:
        name = name.decode("utf-8", "ignore").encode("utf-8")  # 去尾半个多字节字符
    meta[0] = (len(name) + 1) & 0xFF
    meta[1] = 0x7F
    meta[2:2 + len(name)] = name
    if timestamp is None:
        import datetime as _dt
        timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts = timestamp.encode("ascii", "ignore")[:19]
    meta[512] = 0x13
    meta[513:513 + len(ts)] = ts
    struct.pack_into("<I", meta, 768, len(raw) & 0xFFFFFFFF)
    struct.pack_into("<I", meta, 772, len(file_z) & 0xFFFFFFFF)
    struct.pack_into("<I", meta, 780, zlib.crc32(raw) & 0xFFFFFFFF)
    meta[785] = 0x02  # [784:788] = 00 02 00 00
    meta_z = zlib.compress(bytes(meta))
    tail = struct.pack("<IIIIII",
                       zlib.crc32(file_z + meta_z) & 0xFFFFFFFF,
                       1, 1, len(meta_z), len(file_z), 1) + b"\x00" * 12
    return b"\x01" + file_z + meta_z + tail


def _log_cpc_structure(payload: bytes) -> None:
    """诊断：下载到的 .cpc 是否符合原版双流结构（含 788B 元数据块 + 36B 尾）。

    只写日志不改变行为。缺元数据块 = 该文件由旧版桥接上传，原版客户端打开会报「解压失败」。
    """
    try:
        d = zlib.decompressobj()
        fraw = d.decompress(payload)
        fz = len(payload) - len(d.unused_data)
        rest = d.unused_data
        if len(rest) < 40:
            log.info("cpc 结构: 单流 file_z=%dB (无元数据块/尾 → 原版客户端打开会报解压失败)", fz)
            return
        d2 = zlib.decompressobj()
        mraw = d2.decompress(rest)
        mz = len(rest) - len(d2.unused_data)
        tail = rest[mz:]
        name = mraw[2:114].split(b"\x00")[0].decode("utf-8", "ignore") if len(mraw) >= 114 else ""
        tstr = mraw[513:533].split(b"\x00")[0].decode("ascii", "ignore") if len(mraw) >= 533 else ""
        ok_tail = len(tail) == 36
        log.info("cpc 结构: 完整双流 file=%dB(file_z=%d) meta=%dB(meta_z=%d) tail=%dB 名称=%r 时间=%s",
                 len(fraw), fz, len(mraw), mz, len(tail), name, tstr)
        if ok_tail and len(mraw) == 788:
            import struct as _s
            size = _s.unpack("<I", mraw[768:772])[0]
            zlen = _s.unpack("<I", mraw[772:776])[0]
            crc = _s.unpack("<I", mraw[780:784])[0]
            tcrc = _s.unpack("<I", tail[0:4])[0]
            want = zlib.crc32(payload[:fz + mz]) & 0xFFFFFFFF
            log.info("cpc 校验: size=%d(zlen=%d) crc=%d tail_crc=%s 文件自洽=%s",
                     size, zlen, crc,
                     "OK" if tcrc == want else f"MISMATCH({tcrc}!={want})",
                     (size == len(fraw) and zlen == fz
                      and crc == (zlib.crc32(fraw) & 0xFFFFFFFF)))
    except Exception as e:  # 诊断失败不影响主流程
        log.debug("cpc 结构诊断跳过: %s", e)


CPC_TAIL_SIZE = 36  # 原版 .cpc 尾段长度（3 样本一致）
CPC_NAME_LEGACY_CAP = 112  # ⚠ 旧 build_cpc 误用的名字截断上限（09-09 修正前），
                           #   仅用于识别历史坏体：元数据名恰好 112B 且是文档名前缀 = 截断


def parse_cpc_tail(tail: bytes, filesize: int) -> dict:
    """用 36B 尾段判定存储体是否完整（廉价体检，无需下载整个文件）。

    尾段布局（LE u32）：[0:4]=crc32(file_z+meta_z) [4:8]=1 [8:12]=1
                       [12:16]=meta_z 长度 [16:20]=file_z 长度 [20:24]=1 [24:36]=00*12
    判定为 complete 需同时满足：
      ① [24:36] 全零  ② [4:8]/[8:12]/[20:24] 均为 1
      ③ file_z + meta_z + 36 == filesize（自洽，最强判据）

    坏体（旧版桥接上传的 0x01+zlib(文件) 单流）尾部是 zlib 数据，三条全不满足。
    """
    out = {"verdict": "unknown", "file_z": 0, "meta_z": 0, "tail_crc": 0}
    if len(tail) < CPC_TAIL_SIZE or filesize <= 0:
        out["detail"] = f"尾段不足 {CPC_TAIL_SIZE}B 或 filesize 未知"
        return out
    t = tail[:CPC_TAIL_SIZE]
    zeros12 = t[24:36] == bytes(12)
    ones = (t[4:8] == b"\x01\x00\x00\x00" and t[8:12] == b"\x01\x00\x00\x00"
            and t[20:24] == b"\x01\x00\x00\x00")
    meta_z = struct.unpack("<I", t[12:16])[0]
    file_z = struct.unpack("<I", t[16:20])[0]
    size_ok = (file_z + meta_z + CPC_TAIL_SIZE) == filesize
    out.update(file_z=file_z, meta_z=meta_z,
               tail_crc=struct.unpack("<I", t[0:4])[0],
               zeros12=zeros12, ones=ones, size_consistent=size_ok)
    if zeros12 and ones and size_ok:
        out["verdict"] = "complete"
        out["detail"] = f"完整原版体 file_z={file_z} meta_z={meta_z}"
    elif not (zeros12 or ones or size_ok):
        out["verdict"] = "legacy_single_stream"
        out["detail"] = ("尾段无原版结构（旧版桥接上传的单流 .cpc）→ "
                         "原版客户端打开会报「解压失败」，需重新检入修复")
    else:
        out["verdict"] = "suspicious"
        out["detail"] = (f"部分特征命中 zeros12={zeros12} ones={ones} "
                         f"size_ok={size_ok}（file_z={file_z} meta_z={meta_z} filesize={filesize}）")
    return out


def decode_file_body(data: bytes, expected_size: int = 0) -> bytes:
    """解析 fileman 下载响应体 → 真实文件字节。

    体结构：0x03 0x01 0x00(3字节头) + 压缩负载。
    压缩负载可能是 ZIP(PK\\x03\\x04) 或 zlib(78 9c / 78 01 / 78 da)。

    expected_size：getdownstatus 登记的原文件字节数（>0 时启用防误剥守卫）。
    2026-10-30 实证（docid 6493188）：legacy 单流 .cpc 的负载**直接就是用户
    上传的原文件**（无包装层）。若原文件本身是个 zip 且恰好只有 1 个成员，
    旧逻辑会把它误当「.cpc 包装 zip」多剥一层，把里面的 xlsm/xlsx 解出来，
    用户拿到的「xxx.zip」实为内部文档字节。守卫：单成员解包结果长度必须
    等于登记原文件大小，否则说明该 zip 就是原文件本体，原样返回。
    """
    raw = data
    if raw[:3] == b"\x03\x01\x00":
        payload = raw[3:]
    else:
        payload = raw
    # 2) zlib 流（Office 文档/压缩包常走 zlib）
    if payload[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda"):
        _log_cpc_structure(payload)
        try:
            d = zlib.decompressobj()
            return d.decompress(payload) + d.flush()
        except Exception:
            return payload
    # 1) ZIP 容器 / Office 文档（OOXML 本身就是 ZIP）
    pk = payload.find(b"PK\x03\x04")
    if pk != -1:
        zip_payload = payload[pk:]
        try:
            z = zipfile.ZipFile(io.BytesIO(zip_payload))
            names = z.namelist()
            if not names:
                return zip_payload
            # 单成员 ZIP：一般是 .cpc 把单个原文件再包一层，解压出原文件。
            # 但须防「用户上传的 zip 本身只有 1 个成员」被误剥——用登记的
            # 原文件大小校验：解出的成员 ≠ 登记大小 → 该 zip 就是原文件。
            if len(names) == 1:
                member = z.read(names[0])
                if expected_size > 0 and len(member) != expected_size:
                    return zip_payload
                return member
            # 多成员 ZIP：可能是真正的 Office 文档(OOXML)，也可能是用户上传的压缩包。
            # 若像 Office 文档，保留整个 ZIP；否则也保留整个压缩包。
            if _looks_like_ooxml(z):
                return zip_payload
            return zip_payload
        except Exception:
            return payload
    # 3) 其它：原样返回
    return payload


def _looks_like_ooxml(z: zipfile.ZipFile) -> bool:
    """OOXML 文档(zip) 必然包含 [Content_Types].xml，且带有 word/xl/ppt 等目录。"""
    names = set(z.namelist())
    if "[Content_Types].xml" not in names:
        return False
    # 任一 Office 主目录即可判定
    return any(
        p in names
        for p in (
            "word/document.xml",
            "xl/workbook.xml",
            "ppt/presentation.xml",
        )
    )


def parse_bomd_response(xml_text: str):
    """解析 cpcbom.select 响应 → (主表 dict, 子件 dict 列表)。

    响应结构：<response><data>{主表字段}<bomofboms/>...<bomds><bomd>...81字段...</bomd>...</bomds></data></response>
    子件 bomd 字段比 selectref 的 bomofbom 完整（materialcatname/eptag/hasrefdoc/
    lifecycle/costrollup/bomddef*/saleunit/stockunit/basetime/ctrltime/ecnid 等）。
    """
    from lxml import etree
    root = etree.fromstring(xml_text.encode("utf-8", errors="ignore"))
    data = root.find("data")
    if data is None:
        return {}, []
    def _kids(el):
        return {etree.QName(c).localname: (c.text or "") for c in el}
    master = {etree.QName(c).localname: (c.text or "") for c in data
              if etree.QName(c).localname not in ("bomofboms", "bomds")}
    children = []
    bd = data.find("bomds")
    if bd is not None:
        for el in bd.iterfind("bomd"):
            children.append(_kids(el))
    return master, children


if __name__ == "__main__":
    import os

    async def main():
        s = Settings()
        # 2026-09-02：默认账号改为空（不再硬编码「唐阳」），请通过环境变量 SINOCC_ACCOUNT 指定
        c = SinoCCZipClient(s, account=os.environ.get("SINOCC_ACCOUNT", ""),
                            password=os.environ.get("SINOCC_PASSWORD", "Ty18780423647#"))
        try:
            uid = await c.ensure_login()
            print("LOGIN OK loginUID =", uid[:40], "...")
            print("\n=== 测试1: cpcwf.selectall (列表查询) ===")
            r = await c.wf_list()
            print(r[:1200])
        except Exception as e:
            import traceback; traceback.print_exc()
        finally:
            await c.close()

    asyncio.run(main())
