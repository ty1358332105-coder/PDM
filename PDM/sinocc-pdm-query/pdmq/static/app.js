/* SinoCC PDM 查询工具 —— 前端逻辑（流程查询 / 确认单查询）
 *
 * 口径与「SinoCC PDM 桥接」完全一致：
 *   流程查询 = 多层 BOM + 每行物料在途（stat 2~6）的样品确认单/变更单
 *   确认单查询 = 多层 BOM + 每行物料全部确认单（TC 临时 / SC 正式 + 供应商）
 * 差异只在交互：长查询走 job + 确定性进度条，可中途停止。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const API = (location.origin && /^https?:/.test(location.origin)) ? location.origin : "http://127.0.0.1:9130";

/* =============================== 状态 =============================== */
const S = {
  mode: "rels",            // rels | samconfirms
  logged: false,
  user: "",
  busy: false,
  jobId: null,
  pollTimer: null,
  jobStart: 0,
  data: null,              // 当前结果
  stack: [],               // 下钻栈 [{code, maxlayer, months}]
  view: { search: "", sortKey: "", sortDir: 1, filters: {}, onlyHas: true },
  history: [],
};

const MODES = {
  rels: {
    label: "流程查询",
    endpoint: "/api/query/rels",
    stages: ["读 BOM 明细", "扫物料关联对象", "读流程状态"],
    onlyLabel: "只看在途流程",
    empty: "流程查询：多层 BOM + 每行物料在途的样品确认单 / 变更单",
  },
  samconfirms: {
    label: "确认单查询",
    endpoint: "/api/query/samconfirms",
    stages: ["读 BOM 明细", "扫采购件确认单", "读确认单明细"],
    onlyLabel: "只看有确认单",
    empty: "确认单查询：整机 BOM 里受控时间在窗内的采购件（P/OP），逐行列出全部确认单（TC 临时 / SC 正式 + 供应商）；一张确认单都没有的行标红",
  },
};

/* 列定义（与桥接一致） */
const COLS_RELS = [
  ["wbs", "层级"], ["itemcode", "物料编码"], ["itemname", "中文名称"],
  ["samconfirms", "流程中样品确认单"], ["ecns", "流程中变更流程单"],
  ["drawid", "代号"],
  ["itemunit", "计量单位"], ["qty", "数量"], ["purchflag", "采购标识"],
  ["basetime", "基础时间"], ["ctrltime", "受控时间"], ["ecnid", "变更单Id"],
  ["ecncode", "变更单号"], ["efftime", "生效时间"],
  ["prodtype", "生产类型"], ["productmodel", "产品型号"], ["spec", "规格"],
  ["material", "材质"], ["materialcatname", "研发分类"],
  ["itemdesc", "中文描述"], ["rev", "版本"], ["weight", "重量"],
  ["drawsize", "图幅"], ["note", "备注"],
];
const COLS_SAMQ = [
  ["wbs", "层级"], ["itemcode", "物料编码"], ["itemname", "中文名称"],
  ["temp", "临时确认（TC）"], ["formal", "正式确认（SC）"],
  ["has_conf", "确认单"], ["purchflag", "采购标识"],
  ["drawid", "代号"], ["qty", "数量"], ["ctrltime", "受控时间"],
];
const PROD_TYPE = { P: "300采购物料", SA: "300子装配件", PH: "300虚拟物料", OP: "300委外加工物料" };

function cols() { return S.mode === "rels" ? COLS_RELS : COLS_SAMQ; }

/* =============================== 小工具 =============================== */
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function toast(msg, kind) {
  const host = $("toast-host");
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 260); }, kind === "err" ? 6000 : 3200);
}
async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" }, cache: "no-store" };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(API + path, opt);
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch (_) {}
  return { status: r.status, data, text };
}
function fmtMs(ms) { return ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms"; }
function nowStr() { const d = new Date(); return d.toTimeString().slice(0, 8); }

/* =============================== 登录 =============================== */
function renderLogin(st) {
  S.logged = !!st.logged_in;
  S.user = st.user || "";
  const dot = $("login-dot"), txt = $("login-text");
  if (S.logged) {
    dot.className = "dot ok";
    txt.textContent = (st.user || "已登录") + (st.entcode ? " · " + st.entcode : "");
    $("login-pill").title = "已登录：" + (st.user || "") + "\n来源：" + (st.source || "—") + "\n" + (st.login_uid_mask || "");
  } else {
    dot.className = "dot err";
    txt.textContent = "未登录";
    $("login-pill").title = st.reason || "未登录";
  }
  $("login-status-line").innerHTML = S.logged
    ? "<b>当前登录：</b>" + escapeHtml(st.user || "") + " · " + escapeHtml(st.entcode || "") +
      " <span style='color:#8b98a5'>（" + escapeHtml(st.source || "") + "，" + escapeHtml(st.login_uid_mask || "") + "）</span>"
    : "<b style='color:#c62828'>未登录：</b>" + escapeHtml(st.reason || "请先登录");
}
async function loadStatus() {
  const r = await api("GET", "/api/auth/status");
  if (r.status === 200 && r.data) { renderLogin(r.data); return r.data; }
  return { logged_in: false };
}
async function autoLogin(silent) {
  const t0 = Date.now();
  $("login-text").textContent = "检测登录态…";
  $("login-dot").className = "dot";
  try {
    const r = await api("POST", "/api/auth/auto");
    if (r.status === 200 && r.data && r.data.logged_in) {
      renderLogin(r.data);
      if (!silent) toast("自动登录成功：" + (r.data.user || ""), "ok");
      return true;
    }
    const msg = (r.data && (r.data.reason || r.data.detail)) || ("HTTP " + r.status);
    renderLogin({ logged_in: false, reason: msg });
    if (!silent) toast("自动登录未成功：" + msg, "err");
    return false;
  } catch (e) {
    renderLogin({ logged_in: false, reason: "网络异常：" + e.message });
    if (!silent) toast("自动登录异常：" + e.message, "err");
    return false;
  } finally {
    const ms = Date.now() - t0;
    if (ms > 400) $("meta-line").textContent = $("meta-line").textContent; // 保持
  }
}
async function ensureLogin() {
  if (S.logged) return true;
  const ok = await autoLogin(true);
  if (!ok) { openLogin(); toast("未登录：请先打开并登录 SinoCC 客户端，或用密码登录", "err"); }
  return ok;
}

function openLogin() { $("login-overlay").style.display = "flex"; }
function closeOverlay(id) { $(id).style.display = "none"; }

/* =============================== 查询 =============================== */
function setBusy(b) {
  S.busy = b;
  $("btn-run").disabled = b;
  $("btn-run").textContent = b ? "查询中…" : "开始查询";
  $("btn-stop").style.display = b ? "" : "none";
  $("btn-back").style.display = S.stack.length ? "" : "none";
}

function renderStages(modeKey, idx, status) {
  const names = MODES[modeKey].stages;
  $("stages").innerHTML = names.map((n, i) => {
    const no = i + 1;
    let cls = "stage";
    if (status === "done") cls += " done";
    else if (no < idx) cls += " done";
    else if (no === idx) cls += " active";
    return '<span class="' + cls + '"><span class="stage-ico">' + (no < idx || status === "done" ? "" : "●") + "</span>" +
      no + ". " + escapeHtml(n) + "</span>";
  }).join("");
}

function renderProgress(snap) {
  const pct = Math.max(0, Math.min(100, snap.pct || 0));
  $("bar-fill").style.width = pct + "%";
  $("pg-pct").textContent = pct.toFixed(0) + "%";
  renderStages(snap.kind, snap.stage_index, snap.status);
  $("pg-stage").textContent = snap.stage || "";
  $("pg-counter").textContent = snap.total > 0 ? (snap.done + " / " + snap.total) : "";
  $("pg-msg").textContent = snap.message || "";
  const elapsed = snap.elapsed_ms || 0;
  let eta = "";
  if (snap.status === "running" && pct > 4) {
    const total = elapsed / (pct / 100);
    const left = Math.max(0, total - elapsed);
    eta = "已用 " + fmtMs(elapsed) + " · 预计剩余 " + fmtMs(Math.round(left));
  } else if (snap.status !== "running") {
    eta = "用时 " + fmtMs(elapsed);
  }
  $("pg-eta").textContent = eta;
  $("progress-log").textContent = (snap.log || []).join("\n");
  $("progress-log").scrollTop = $("progress-log").scrollHeight;
}

async function startQuery(opts) {
  if (!(await ensureLogin())) return;
  const code = (opts && opts.code) || $("q-code").value.trim();
  if (!code) { $("qb-hint").textContent = "请输入物料编码"; $("q-code").focus(); return; }
  $("qb-hint").textContent = "";
  const body = {
    itemcode: code,
    maxlayer: (opts && opts.maxlayer) || $("q-layer").value,
    months: parseInt($("q-months").value, 10) || 0,
  };
  // 确认单查询：只查采购件（后端默认 P,OP，这里显式带上，口径写在 UI 提示里）
  if (S.mode === "samconfirms") body.purchflags = "P,OP";
  // 下钻：记栈
  if (opts && opts.drill) S.stack.push({ code, maxlayer: body.maxlayer, months: body.months });
  else if (!(opts && opts.keepStack)) S.stack = [];
  // 确认单查询默认「显示全部采购件」——只有把缺确认的行也列出来，才谈得上标色
  const defaultOnlyHas = S.mode === "rels";
  S.view = { search: "", sortKey: "", sortDir: 1, filters: {}, onlyHas: defaultOnlyHas, onlyMissing: false };
  $("tb-search").value = "";
  $("tb-onlychk").checked = defaultOnlyHas;
  $("tb-miss").checked = false;
  $("tb-onlychk-label").textContent = MODES[S.mode].onlyLabel;
  $("tb-onlychk-wrap").style.display = "";
  $("tb-miss-wrap").style.display = S.mode === "samconfirms" ? "" : "none";
  updateCrumb();

  setBusy(true);
  $("progress-wrap").style.display = "";
  $("result-toolbar").style.display = "none";
  $("table-host").innerHTML = '<div class="empty"><div class="empty-icon">⏳</div><div class="empty-title">查询中…</div>' +
    '<div class="empty-sub">' + escapeHtml(code) + "（" + escapeHtml(MODES[S.mode].label) + "，层级 " +
    escapeHtml(body.maxlayer === "0" ? "全部" : body.maxlayer) + "，时间窗 " +
    (body.months ? body.months + " 月" : "不限") + "）</div></div>";
  $("stages").innerHTML = "";
  $("progress-log").textContent = "";
  renderProgress({ kind: S.mode, stage: "提交任务…", stage_index: 1, done: 0, total: 0, pct: 0, elapsed_ms: 0, log: [] });

  const r = await api("POST", MODES[S.mode].endpoint, body);
  if (r.status === 401) { setBusy(false); openLogin(); toast("会话失效，请重新登录", "err"); return; }
  if (r.status !== 200 || !r.data || !r.data.ok) {
    setBusy(false);
    $("table-host").innerHTML = '<div class="empty"><div class="empty-title" style="color:#c62828">查询启动失败</div>' +
      '<div class="empty-sub">' + escapeHtml((r.data && r.data.detail) || r.text || r.status) + "</div></div>";
    return;
  }
  S.jobId = r.data.job_id;
  S.jobStart = Date.now();
  pollJob();
}

function pollJob() {
  clearTimeout(S.pollTimer);
  S.pollTimer = setTimeout(async () => {
    if (!S.jobId) return;
    const r = await api("GET", "/api/jobs/" + S.jobId);
    if (r.status !== 200 || !r.data) { setBusy(false); toast("进度查询失败", "err"); return; }
    const snap = r.data;
    renderProgress(snap);
    if (snap.status === "running") { pollJob(); return; }
    if (snap.status === "cancelled") { setBusy(false); toast("已停止查询"); return; }
    if (snap.status === "error") {
      setBusy(false);
      $("table-host").innerHTML = '<div class="empty"><div class="empty-title" style="color:#c62828">查询失败</div>' +
        '<div class="empty-sub">' + escapeHtml(snap.error || "") + "</div></div>";
      toast("查询失败：" + (snap.error || ""), "err");
      return;
    }
    // done → 取结果
    const rr = await api("GET", "/api/jobs/" + S.jobId + "/result");
    setBusy(false);
    if (rr.status === 401) { openLogin(); toast("会话失效，请重新登录", "err"); return; }
    if (rr.status !== 200 || !rr.data || !rr.data.ok) {
      $("table-host").innerHTML = '<div class="empty"><div class="empty-title" style="color:#c62828">结果获取失败</div>' +
        '<div class="empty-sub">' + escapeHtml((rr.data && rr.data.detail) || rr.text || rr.status) + "</div></div>";
      return;
    }
    S.data = rr.data;
    renderResult();
    if (snap.params && snap.params.itemcode) addHistory(snap.params, snap.elapsed_ms);
    // 完成后收起进度区（结果工具栏已有「⚡ 总 xxx」），把高度还给表格；出错/停止则保留
    setTimeout(() => { if (!S.busy) $("progress-wrap").style.display = "none"; }, 1400);
  }, 380);
}

function stopQuery() {
  if (!S.jobId) return;
  api("POST", "/api/jobs/" + S.jobId + "/cancel");
  toast("正在停止…");
}

/* =============================== 结果渲染 =============================== */
function bomLevel(r) {
  const w = String(r.wbs == null ? "" : r.wbs);
  if (!w) return 0;
  return (w.match(/\./g) || []).length;
}
function wbsKey(r) {
  return String(r.wbs == null ? "" : r.wbs).split(".").map(n => parseInt(n, 10) || 0);
}
function cmpWbs(a, b) {
  const x = wbsKey(a.raw || a), y = wbsKey(b.raw || b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] || 0) - (y[i] || 0);
    if (d) return d;
  }
  return 0;
}
function cellText(key, r) {
  if (key === "prodtype") return PROD_TYPE[r.purchflag] || "";
  const v = r[key];
  return v === undefined || v === null ? "" : String(v);
}

/* 行数据装配：BOM 行 + 本行确认单（两种查询共用） */
function buildRows() {
  const d = S.data || {};
  const rows = d.rows || [];
  if (S.mode === "rels") {
    const rels = d.active_rels || {};
    return rows.map(r => {
      const code = String(r.itemcode || "").trim();
      const list = code ? (rels[code] || []) : [];
      return {
        r, code,
        sams: list.filter(o => o.objtypeid === "101"),
        ecns: list.filter(o => o.objtypeid === "91046"),
        temp: [], formal: [], all: list,
      };
    }).filter(it => it.code || it.r.itemname);
  }
  const sam = d.samconfirms || {};
  return rows.map(r => {
    const code = String(r.itemcode || "").trim();
    const list = code ? (sam[code] || []) : [];
    return {
      r, code, sams: [], ecns: [],
      temp: list.filter(c => c.kind === "temp"),
      formal: list.filter(c => c.kind === "formal"),
      all: list,
    };
  }).filter(it => it.code || it.r.itemname);
}

function rowHasFlow(it) {
  return (it.sams.length + it.ecns.length + it.all.length) > 0;
}
function rowSearchText(it) {
  const parts = [it.code, it.r.itemname || "", it.r.drawid || "", it.r.spec || "", it.r.itemdesc || ""];
  it.sams.forEach(o => parts.push(o.objcode || ""));
  it.ecns.forEach(o => parts.push(o.objcode || ""));
  it.all.forEach(o => parts.push((o.code || "") + " " + (o.vendor || "") + " " + (o.createtime || "")));
  return parts.join(" ").toLowerCase();
}
function rowMatchesFilters(it) {
  for (const key in S.view.filters) {
    const v = S.view.filters[key];
    if (!v) continue;
    if (key === "samconfirms") {
      if (v === "__wf:has__" && !it.sams.length) return false;
      if (v === "__wf:none__" && it.sams.length) return false;
      continue;
    }
    if (key === "ecns") {
      if (v === "__wf:has__" && !it.ecns.length) return false;
      if (v === "__wf:none__" && it.ecns.length) return false;
      continue;
    }
    if (key === "temp" || key === "formal" || key === "has_conf") {
      const n = key === "has_conf" ? it.all.length : it[key].length;
      if (v === "__wf:has__" && !n) return false;
      if (v === "__wf:none__" && n) return false;
      continue;
    }
    let txt = "";
    if (key === "samconfirms") txt = it.sams.map(o => o.objcode).join(" ");
    else if (key === "wbs") txt = String(it.r.wbs || "");
    else txt = cellText(key, it.r);
    if (String(txt).toLowerCase().indexOf(v) < 0) return false;
  }
  return true;
}
function sortRows(arr) {
  const k = S.view.sortKey;
  if (!k) return arr.slice().sort(cmpWbs);
  const dir = S.view.sortDir || 1;
  const val = (it) => {
    if (k === "wbs") return wbsKey(it.r);
    if (k === "samconfirms") return it.sams.length;
    if (k === "ecns") return it.ecns.length;
    if (k === "temp") return it.temp.length;
    if (k === "formal") return it.formal.length;
    return String(cellText(k, it.r)).toLowerCase();
  };
  return arr.slice().sort((a, b) => {
    const x = val(a), y = val(b);
    if (Array.isArray(x) && Array.isArray(y)) {
      for (let i = 0; i < Math.max(x.length, y.length); i++) {
        const d = (x[i] || 0) - (y[i] || 0);
        if (d) return d * dir;
      }
      return 0;
    }
    if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
    return String(x).localeCompare(String(y), "zh") * dir;
  });
}

function confBlock(o, cls, objtype) {
  const kindCls = cls || "";
  // objtypeid：101=样品确认单 / 91046=更改通知单。必须带上 —— 两种对象共用同一个
  // objid 数字空间，光凭 objid 猜类型会取到对方的空壳（2026-11-09 实测 91869 打开空白）。
  const ot = String(objtype || (kindCls === "ecn" ? "91046" : "101"));
  const tip = [
    "对象：" + (o.objcode || o.code || "") + "（objid=" + (o.objid || "") + "）",
    o.objtypename ? "类型：" + o.objtypename : (o.type ? "类型：" + o.type : ""),
    o.vendor ? "供应商：" + o.vendor : "",
    o.wfname ? "流程：" + o.wfname + "（stat=" + (o.wfstat || "") + "）" : "",
    (o.objdef1 || o.creator) ? "编制：" + (o.objdef1 || o.creator) + " " + (o.objdef2 || o.createtime || "") : "",
    "",
    "点击查看详情",
  ].filter(Boolean).join("\n");
  const code = o.objcode || o.code || "?";
  const objid = o.objid || "";
  return '<div class="conf-item ' + kindCls + '">' +
    '<a href="javascript:void(0)" data-objid="' + escapeHtml(objid) + '" data-objtype="' + escapeHtml(ot) +
    '" title="' + escapeHtml(tip) + '">' + escapeHtml(code) + "</a>" +
    (o.vendor ? ' <span class="conf-attr">' + escapeHtml(o.vendor) + "</span>" : "") +
    ((o.objdef2 || o.createtime) ? ' <span class="conf-time">' + escapeHtml(o.objdef2 || o.createtime) + "</span>" : "") +
    "</div>";
}

function renderResult() {
  const all = buildRows();
  const d0 = S.data || {};
  // 取数失败的物料：既不能算「有确认单」，也绝不能标红算「无确认单」（那是谎报）
  const failedSet = new Set(((d0.fetch_failed) || []).map(String));
  const kw = (S.view.search || "").toLowerCase();
  const onlyHas = S.view.onlyHas && (S.mode === "rels" || S.mode === "samconfirms");
  const onlyMissing = S.view.onlyMissing && S.mode === "samconfirms";
  let shown = all.filter(rowMatchesFilters);
  if (kw) shown = shown.filter(it => rowSearchText(it).indexOf(kw) >= 0);
  if (onlyHas) shown = shown.filter(rowHasFlow);
  if (onlyMissing) shown = shown.filter(it => !it.all.length && !failedSet.has(String(it.code)));
  shown = sortRows(shown);

  const withFlow = all.filter(rowHasFlow).length;
  const noConf = all.filter(it => !it.all.length && !failedSet.has(String(it.code))).length;
  const nFailed = failedSet.size;
  const d = S.data || {};
  const skipped = d.skipped || {};

  // 工具栏
  $("result-toolbar").style.display = "";
  const isSamq = S.mode === "samconfirms";
  // 时间窗对确认单查询 = 物料受控时间（窗外的物料整行不出现，所以这里显示的就是「窗内」）
  const timeWin = (skipped.months > 0 && skipped.cutoff)
    ? '<span style="color:#1565c0;background:#e3f2fd;padding:2px 7px;border-radius:10px;" title="' +
      (isSamq ? "只列受控时间在窗内的采购件；窗外物料不显示、也不计入缺单统计" : "物料受控时间 + 单据编制时间都在窗内") +
      '">🕒 仅查最近 ' + skipped.months +
      " 月（受控≥" + escapeHtml(skipped.cutoff) + "）" +
      ((skipped.skipped_by_ctrltime || 0) > 0 ? " · 受控超窗 " + skipped.skipped_by_ctrltime : "") +
      ((skipped.skipped_by_objtime || 0) > 0 ? " · 单据超期 " + skipped.skipped_by_objtime : "") +
      ((isSamq && (skipped.empty_ctrltime || 0) > 0) ? " · 受控时间空 " + skipped.empty_ctrltime : "") + "</span>"
    : '<span style="color:#546e7a;background:#eceff1;padding:2px 7px;border-radius:10px;">🕒 时间窗：不限</span>';
  const purchChip = isSamq
    ? '<span style="color:#00695c;background:#e0f2f1;padding:2px 7px;border-radius:10px;" title="确认单查询只查采购件（采购标识 P / OP）；编码以 P 开头的物料不查确认单、不参与缺单判定">📦 仅采购件 ' +
      escapeHtml((skipped.purchflags || ["P", "OP"]).join("/")) +
      ((skipped.skipped_by_purchflag || 0) > 0 ? " · 已排除其它 " + skipped.skipped_by_purchflag : "") +
      ((skipped.skipped_by_prefix || 0) > 0 ? " · P前缀 " + skipped.skipped_by_prefix : "") + "</span>"
    : "";
  const missChip = isSamq
    ? '<span class="miss-chip" style="color:#c0392b;background:#fdecea;padding:2px 7px;border-radius:10px;" title="窗内受控的采购件里，一张 TC/SC 都没有的行（整行标红）">🟥 无确认单 ' +
      noConf + " 行</span>" +
      (nFailed > 0
        ? '<span style="color:#8d6e00;background:#fff8e1;padding:2px 7px;border-radius:10px;" title="这些物料取关联对象时出错（多为服务端偶发），没有判定为无确认单；重新查询一次即可">⚠ 取数失败 ' +
          nFailed + " 行</span>"
        : "")
    : "";
  const perf = d.perf_ms
    ? '<span style="color:#455a64;background:#f4f8fb;border:1px solid #d7e3ee;padding:2px 7px;border-radius:10px;">⚡ 总 ' +
      fmtMs(d.perf_ms.total_ms || 0) + "</span>"
    : "";
  const head = S.mode === "rels"
    ? "🧭 有在途流程 " + withFlow + " / 共 " + all.length + " 行 · 最大 " + escapeHtml(d.maxlayer || "?") + " 层"
    : "🧾 有确认单 " + withFlow + " / 共 " + all.length + " 行采购件 · 确认单 " +
      all.reduce((a, it) => a + it.all.length, 0) + " 张 · 最大 " + escapeHtml(d.maxlayer || "?") + " 层";
  $("tb-info").innerHTML = "<span>" + head + "</span>" + purchChip + missChip + timeWin + perf;
  $("tb-count").textContent = shown.length + " / " + all.length + " 行";
  const hasFilter = S.view.search || Object.keys(S.view.filters).length ||
    (S.view.onlyHas && all.length !== shown.length) || onlyMissing;
  $("tb-clear").style.display = hasFilter ? "" : "none";

  // 表格
  const C = cols();
  let html = '<table><thead><tr>';
  C.forEach(c => {
    const key = c[0];
    const active = S.view.sortKey === key;
    const filtered = !!S.view.filters[key];
    html += '<th data-key="' + escapeHtml(key) + '" class="' + (active ? "active" : "") +
      '" title="左键排序，右键筛选该列">' + escapeHtml(c[1]) +
      '<span class="sort-ind">' + (active ? (S.view.sortDir > 0 ? "▲" : "▼") : (filtered ? "◎" : "↕")) + "</span></th>";
  });
  html += "</tr></thead><tbody>";
  shown.forEach((it, idx) => {
    const r = it.r, lv = bomLevel(r);
    // 确认单查询：期内一张 TC/SC 都没有的采购件整行标红（用户要求「标颜色出来」）
    const failed = failedSet.has(String(it.code));
    const miss = isSamq && !it.all.length && !failed;
    const rowBg = failed ? "#fff8e1" : (miss ? "#fdecea" : (lv === 0 ? "#f3f9ff" : (lv % 2 === 0 ? "#ffffff" : "#fafcfe")));
    html += '<tr data-idx="' + idx + '" data-code="' + escapeHtml(it.code) + '"' +
      (miss ? ' class="row-miss"' : (failed ? ' class="row-failed"' : "")) + ' style="background:' + rowBg + '">';
    C.forEach(c => {
      const key = c[0];
      let cls = "", txt = "";
      if (key === "wbs") { cls = "cell-wbs"; txt = escapeHtml(r.wbs || ""); }
      else if (key === "itemcode") { cls = "cell-code" + (miss ? " miss-text" : ""); txt = escapeHtml(it.code); }
      else if (key === "itemname") {
        const dot = lv === 0 ? "●" : (lv === 1 ? "◆" : (lv === 2 ? "◇" : "•"));
        const col = ["#2e7d32", "#0b5aa6", "#6a1b9a", "#00726b"][Math.min(lv, 3)];
        txt = '<span style="color:' + col + '">' + dot + "</span> " + escapeHtml(r.itemname || "");
        if (miss) txt += ' <span class="miss-badge">无确认单</span>';
        if (failed) txt += ' <span class="fail-badge">取数失败</span>';
        cls = "cell-name";
      } else if (key === "samconfirms") {
        cls = "cell-wrap";
        txt = it.sams.length ? it.sams.map(o => confBlock(o, "")).join("") : '<span style="color:#cfd8dc">—</span>';
      } else if (key === "ecns") {
        cls = "cell-wrap";
        txt = it.ecns.length ? it.ecns.map(o => confBlock(o, "ecn")).join("") : '<span style="color:#cfd8dc">—</span>';
      } else if (key === "temp") {
        cls = "cell-wrap";
        txt = it.temp.length ? it.temp.map(o => confBlock(o, "temp")).join("")
          : (miss ? '<span class="miss-dash">—</span>' : '<span style="color:#cfd8dc">—</span>');
      } else if (key === "formal") {
        cls = "cell-wrap";
        txt = it.formal.length ? it.formal.map(o => confBlock(o, "formal")).join("")
          : (miss ? '<span class="miss-dash">—</span>' : '<span style="color:#cfd8dc">—</span>');
      } else if (key === "has_conf") {
        cls = "cell-flag";
        txt = failed ? '<span class="flag-unknown">?</span>'
          : (it.all.length ? '<span class="flag-yes">有 ' + it.all.length + "</span>"
                           : '<span class="flag-no">无</span>');
      } else if (key === "qty" || key === "weight") { cls = "cell-num"; txt = escapeHtml(cellText(key, r)); }
      else if (key === "drawid" || key === "ecncode") { cls = "cell-code"; txt = escapeHtml(cellText(key, r)); }
      else if (key === "itemdesc" || key === "note" || key === "materialcatname") { cls = "cell-wrap"; txt = escapeHtml(cellText(key, r)); }
      else { txt = escapeHtml(cellText(key, r)); }
      html += '<td class="' + cls + '"' + (key === "itemname" ? ' style="padding-left:' + (8 + lv * 16) + 'px"' : "") + ">" + txt + "</td>";
    });
    html += "</tr>";
  });
  if (!shown.length) {
    html += '<tr><td colspan="' + C.length + '" style="padding:36px;text-align:center;color:#8b98a5">' +
      (all.length ? "没有匹配的行（可取消勾选「" + MODES[S.mode].onlyLabel + "」查看全部物料）" : "该物料无 BOM 子件") +
      "</td></tr>";
  }
  html += "</tbody></table>";
  $("table-host").innerHTML = html;
  bindTableEvents();
}

function bindTableEvents() {
  const host = $("table-host");
  host.querySelectorAll("th[data-key]").forEach(th => {
    th.onclick = () => {
      const key = th.dataset.key;
      if (S.view.sortKey === key) {
        S.view.sortDir = S.view.sortDir > 0 ? -1 : 0;
        if (S.view.sortDir === 0) S.view.sortKey = "";
      } else { S.view.sortKey = key; S.view.sortDir = 1; }
      renderResult();
    };
    th.oncontextmenu = (e) => { e.preventDefault(); openColMenu(e.clientX, e.clientY, th.dataset.key, th.textContent.replace(/[↕▲▼◎]/g, "").trim()); };
  });
  host.querySelectorAll("tbody tr[data-idx]").forEach(tr => {
    tr.oncontextmenu = (e) => { e.preventDefault(); openRowMenu(e.clientX, e.clientY, tr.dataset.code); };
    tr.ondblclick = (e) => {
      if (e.target.closest("a")) return;
      const code = tr.dataset.code;
      if (!code) return;
      startQuery({ code, maxlayer: "0", drill: true });
    };
  });
  host.querySelectorAll("a[data-objid]").forEach(a => {
    a.onclick = (e) => { e.stopPropagation(); openObject(a.dataset.objid, a.dataset.objtype); };
  });
}

/* =============================== 列筛选 / 行菜单 =============================== */
let _colMenuKey = null;
function openColMenu(x, y, key, label) {
  const cur = S.view.filters[key] || "";
  const isFlow = ["samconfirms", "ecns", "temp", "formal", "has_conf"].indexOf(key) >= 0;
  const menu = $("colmenu");
  let body;
  if (isFlow) {
    body = '<div class="cm-title">' + escapeHtml(label) + "：有无筛选</div>" +
      '<div class="cm-row" style="flex-direction:column;gap:5px;">' +
      '<button data-act="has" style="' + (cur === "__wf:has__" ? "background:#0b5aa6;color:#fff;border-color:#0b5aa6;" : "") + '">● 有（该行存在' + escapeHtml(label) + "）</button>" +
      '<button data-act="none" style="' + (cur === "__wf:none__" ? "background:#c62828;color:#fff;border-color:#c62828;" : "") + '">○ 无（该行没有' + escapeHtml(label) + "）</button>" +
      "</div>";
  } else {
    body = '<div class="cm-title">' + escapeHtml(label) + " 筛选</div>" +
      '<input id="cm-input" placeholder="模糊包含…" value="' + escapeHtml(cur) + '" />' +
      '<div class="cm-row"><button data-act="filter">筛选</button><button data-act="asc">▲ 升序</button><button data-act="desc">▼ 降序</button></div>';
  }
  body += '<div class="cm-row" style="margin-top:6px;"><button data-act="clear" style="color:#c62828;border-color:#efc4c4;">清除本列筛选</button></div>';
  menu.innerHTML = body;
  menu.style.left = Math.min(x, window.innerWidth - 240) + "px";
  menu.style.top = Math.min(y, window.innerHeight - 190) + "px";
  menu.style.display = "";
  _colMenuKey = key;
  const inp = $("cm-input");
  if (inp) { inp.focus(); inp.onkeydown = (e) => { if (e.key === "Enter") applyColMenu("filter"); }; }
  menu.querySelectorAll("button").forEach(b => b.onclick = () => applyColMenu(b.dataset.act));
}
function applyColMenu(act) {
  const key = _colMenuKey;
  const menu = $("colmenu");
  if (act === "has") S.view.filters[key] = "__wf:has__";
  else if (act === "none") S.view.filters[key] = "__wf:none__";
  else if (act === "filter") {
    const v = ($("cm-input").value || "").trim().toLowerCase();
    if (v) S.view.filters[key] = v; else delete S.view.filters[key];
  } else if (act === "asc") { S.view.sortKey = key; S.view.sortDir = 1; }
  else if (act === "desc") { S.view.sortKey = key; S.view.sortDir = -1; }
  else if (act === "clear") { delete S.view.filters[key]; }
  menu.style.display = "none";
  renderResult();
}
function openRowMenu(x, y, code) {
  const menu = $("rowmenu");
  menu.innerHTML =
    '<div style="font-size:11px;color:#8b98a5;margin-bottom:5px;">' + escapeHtml(code || "") + "</div>" +
    '<button data-act="drill">展开下级 BOM 明细</button>' +
    '<button data-act="item">查看物料属性</button>' +
    '<button data-act="copy">复制物料编码</button>';
  menu.style.left = Math.min(x, window.innerWidth - 200) + "px";
  menu.style.top = Math.min(y, window.innerHeight - 140) + "px";
  menu.style.display = "";
  menu.querySelectorAll("button").forEach(b => b.onclick = () => {
    menu.style.display = "none";
    const act = b.dataset.act;
    if (act === "drill") startQuery({ code, maxlayer: "0", drill: true });
    else if (act === "item") openItem(code);
    else if (act === "copy") { navigator.clipboard && navigator.clipboard.writeText(code); toast("已复制：" + code); }
  });
}

/* =============================== 对象 / 物料详情 =============================== */
async function openObject(objid, objtype) {
  if (!objid) return;
  $("obj-overlay").style.display = "flex";
  $("obj-title").textContent = "加载中…";
  $("obj-kind").textContent = objtype === "91046" ? "变更单" : "确认单";
  $("obj-body").innerHTML = '<div style="padding:24px;color:#8b98a5">正在读取对象详情…</div>';
  // 明确知道 objtype 就只打那个接口；不知道才两路都试。
  // （101=101 样品确认单 / 91046=更改通知单，两种对象 objid 会撞号，取错接口会拿到空壳）
  const order = objtype === "91046" ? ["ecn", "samconfirm"]
    : objtype === "101" ? ["samconfirm", "ecn"]
      : ["samconfirm", "ecn"];
  let r = null, lastErr = "";
  for (const kind of order) {
    r = await api("GET", "/api/objects/" + encodeURIComponent(objid) + "/" + kind);
    if (r.status === 200 && r.data && r.data.ok && (r.data.fields || []).length) break;
    lastErr = (r.data && r.data.detail) || r.text || ("HTTP " + r.status);
    r = null;
  }
  if (!r) {
    $("obj-body").innerHTML = '<div style="padding:20px;color:#c62828">' +
      escapeHtml("对象详情读取失败：" + lastErr) + '<div style="margin-top:10px;color:#8b98a5;font-size:12px">' +
      "objid=" + escapeHtml(objid) + " · 类型 " + escapeHtml(objtype || "未知") + "</div></div>";
    return;
  }
  const d = r.data;
  $("obj-kind").textContent = d.kind === "ecn" ? "变更单" : "确认单";
  $("obj-title").textContent = d.code || objid;
  let html = '<div class="kv-grid">';
  (d.fields || []).forEach(p => {
    // 长文本（变更原因/备注/结论）占满整行，否则会把旁边的短字段挤成窄条
    const wide = String(p[1] || "").length > 48 ? " kv-wide" : "";
    html += '<div class="kv' + wide + '"><span class="kv-k">' + escapeHtml(p[0]) + '</span><span class="kv-v">' + escapeHtml(p[1]) + "</span></div>";
  });
  html += "</div>";
  (d.tables || []).forEach(t => {
    html += '<div class="obj-table-title">' + escapeHtml(t.title) + "</div><div class='obj-table-host'><table><thead><tr>";
    t.columns.forEach(c => html += "<th>" + escapeHtml(c) + "</th>");
    html += "</tr></thead><tbody>";
    t.rows.forEach(row => {
      html += "<tr>" + row.map(v => "<td>" + escapeHtml(v) + "</td>").join("") + "</tr>";
    });
    html += "</tbody></table></div>";
  });
  if ((d.attachments || []).length) {
    html += '<div class="obj-table-title">附件（' + d.attachments.length + "）</div><ul class='att-list'>" +
      d.attachments.map(a => "<li>" + escapeHtml(a.docname) + "</li>").join("") + "</ul>";
  }
  $("obj-body").innerHTML = html;
}
async function openItem(code) {
  $("item-overlay").style.display = "flex";
  $("item-title").textContent = code;
  $("item-body").innerHTML = '<div style="padding:20px;color:#8b98a5">读取物料属性…</div>';
  const r = await api("GET", "/api/items/" + encodeURIComponent(code));
  if (r.status !== 200 || !r.data || !r.data.ok) {
    $("item-body").innerHTML = '<div style="padding:16px;color:#c62828">' + escapeHtml((r.data && r.data.detail) || r.status) + "</div>";
    return;
  }
  $("item-title").textContent = (r.data.code || code) + (r.data.name ? " · " + r.data.name : "");
  let html = '<div class="kv-grid" style="grid-template-columns:1fr 1fr;">';
  (r.data.fields || []).forEach(p => {
    html += '<div class="kv"><span class="kv-k">' + escapeHtml(p[0]) + '</span><span class="kv-v">' + escapeHtml(p[1]) + "</span></div>";
  });
  html += "</div>";
  $("item-body").innerHTML = html;
}

/* =============================== 导出 Excel =============================== */
function xlsEsc(s) {
  return String(s == null ? "" : s).replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function xlsWidth(s) {
  let w = 0;
  for (const ch of String(s == null ? "" : s)) w += ch.codePointAt(0) > 0x2e80 ? 2 : 1;
  return w;
}
// UTF-8 安全的 base64（pywebview 落盘桥用；btoa 只认 latin1）
function _b64Utf8(s) {
  const bytes = new TextEncoder().encode(String(s));
  let bin = "";
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  }
  return btoa(bin);
}
function exportExcel(sheet, fileBase, headers, matrix, opts) {
  try {
    const wrapCols = (opts && opts.wrapCols) || [];
    const safe = String(sheet || "导出").replace(/[\\/?*[\]:]/g, "_").slice(0, 31);
    let xml = '<?xml version="1.0" encoding="UTF-8"?>\n<?mso-application progid="Excel.Sheet"?>\n' +
      '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:o="urn:schemas-microsoft-com:office:office"' +
      ' xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">' +
      '<Styles><Style ss:ID="hd"><Font ss:Bold="1"/><Interior ss:Color="#ddebf7" ss:Pattern="Solid"/></Style>' +
      '<Style ss:ID="wrap"><Alignment ss:Vertical="Top" ss:WrapText="1"/></Style></Styles>' +
      '<Worksheet ss:Name="' + xlsEsc(safe) + '"><WorksheetOptions xmlns="urn:schemas-microsoft-com:office:excel">' +
      '<FreezePanes/><SplitHorizontal>1</SplitHorizontal><TopRowBottomPane>1</TopRowBottomPane></WorksheetOptions><Table>';
    headers.forEach((h, ci) => {
      let m = xlsWidth(h);
      matrix.forEach(cells => {
        const v = cells[ci];
        if (v == null) return;
        const w = String(v).split("\n").reduce((a, s) => Math.max(a, xlsWidth(s)), 0);
        m = Math.max(m, w);
      });
      xml += '<Column ss:AutoFitWidth="0" ss:Width="' + Math.round(Math.min(Math.max(m + 4, 12), 90) * 7) + '"/>';
    });
    xml += '<Row ss:StyleID="hd">' + headers.map(h => '<Cell><Data ss:Type="String">' + xlsEsc(h) + "</Data></Cell>").join("") + "</Row>";
    matrix.forEach(cells => {
      xml += "<Row>" + headers.map((h, ci) => {
        const txt = xlsEsc(cells[ci]).replace(/\r?\n/g, "&#10;");
        const st = wrapCols.indexOf(ci) >= 0 ? ' ss:StyleID="wrap"' : "";
        return "<Cell" + st + '><Data ss:Type="String">' + txt + "</Data></Cell>";
      }).join("") + "</Row>";
    });
    xml += "</Table></Worksheet></Workbook>";
    const d = new Date(), p2 = n => String(n).padStart(2, "0");
    const ts = d.getFullYear() + p2(d.getMonth() + 1) + p2(d.getDate()) + "_" + p2(d.getHours()) + p2(d.getMinutes()) + p2(d.getSeconds());
    const fname = String(fileBase || "导出").replace(/[\\/:*?"<>|]/g, "_") + "_" + safe + "_" + ts + ".xls";
    // pywebview（WebView2 原生窗口）不处理 <a download> 的 blob 下载 → 走 JS API 由 Python 写入「下载」目录；
    // 浏览器窗口（Chrome/Edge --app）无 window.pywebview，走原 blob 下载通道。
    const apiSave = (window.pywebview && window.pywebview.api && window.pywebview.api.save_file)
      ? window.pywebview.api.save_file : null;
    if (apiSave) {
      Promise.resolve(apiSave(fname, _b64Utf8(xml))).then(p => {
        if (p) toast("已导出：" + p + "（" + matrix.length + " 行）", "ok");
        else toast("导出失败：保存目录不可写，详见日志", "err");
      }).catch(err => toast("导出失败：" + ((err && err.message) || err), "err"));
      return;
    }
    const blob = new Blob([xml], { type: "application/vnd.ms-excel;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = fname; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast("已导出：" + fname + "（" + matrix.length + " 行）", "ok");
  } catch (e) {
    toast("导出失败：" + e.message, "err");
  }
}
function doExport() {
  const C = cols();
  const all = buildRows();
  const kw = (S.view.search || "").toLowerCase();
  let shown = all.filter(rowMatchesFilters);
  if (kw) shown = shown.filter(it => rowSearchText(it).indexOf(kw) >= 0);
  if (S.view.onlyHas) shown = shown.filter(rowHasFlow);
  shown = sortRows(shown);
  const base = (S.data && S.data.itemcode) || "";
  if (S.mode === "rels") {
    const headers = C.map(c => c[1]);
    const matrix = shown.map(it => C.map(c => {
      const k = c[0];
      if (k === "samconfirms") return it.sams.map(o => o.objcode + " " + (o.objdef1 || "") + " " + (o.objdef2 || "")).join("\n");
      if (k === "ecns") return it.ecns.map(o => o.objcode + " " + (o.objdef1 || "") + " " + (o.objdef2 || "")).join("\n");
      return cellText(k, it.r);
    }));
    exportExcel("流程查询", base, headers, matrix, { wrapCols: [3, 4] });
  } else {
    const headers = ["层级", "物料编码", "中文名称", "临时确认（TC）", "正式确认（SC）",
                     "确认单", "采购标识", "代号", "数量", "受控时间"];
    const matrix = shown.map(it => [
      it.r.wbs || "", it.code, it.r.itemname || "",
      it.temp.map(o => (o.code || "") + (o.vendor ? " " + o.vendor : "")).join("\n"),
      it.formal.map(o => (o.code || "") + (o.vendor ? " " + o.vendor : "")).join("\n"),
      it.all.length ? "有 " + it.all.length : "无",
      it.r.purchflag || "", it.r.drawid || "", it.r.qty || "", it.r.ctrltime || "",
    ]);
    exportExcel("确认单查询", base, headers, matrix, { wrapCols: [3, 4] });
  }
}

/* =============================== 历史 / 面包屑 =============================== */
function addHistory(params, ms) {
  const item = { code: params.itemcode, mode: S.mode, maxlayer: params.maxlayer, months: params.months, ts: Date.now(), ms: ms || 0 };
  S.history = S.history.filter(h => !(h.code === item.code && h.mode === item.mode));
  S.history.unshift(item);
  S.history = S.history.slice(0, 12);
  try { localStorage.setItem("pdmq.history", JSON.stringify(S.history)); } catch (_) {}
  renderHistory();
}
function loadHistory() {
  try { S.history = JSON.parse(localStorage.getItem("pdmq.history") || "[]"); } catch (_) { S.history = []; }
  renderHistory();
}
function renderHistory() {
  const host = $("history-list");
  if (!S.history.length) { host.innerHTML = '<div style="padding:6px;color:#aab6c1;font-size:11px;">暂无</div>'; return; }
  host.innerHTML = S.history.map((h, i) =>
    '<div class="hist-item" data-i="' + i + '">' +
    '<span class="hist-code">' + escapeHtml(h.code) + "</span>" +
    '<span class="hist-meta">' + escapeHtml(h.mode === "rels" ? "流程查询" : "确认单查询") +
    " · " + (h.maxlayer === "0" ? "全部层" : h.maxlayer + " 层") + " · " + (h.months ? h.months + " 月" : "不限") + "</span></div>"
  ).join("");
  host.querySelectorAll(".hist-item").forEach(el => {
    el.onclick = () => {
      const h = S.history[parseInt(el.dataset.i, 10)];
      if (!h) return;
      switchMode(h.mode);
      $("q-code").value = h.code;
      $("q-layer").value = h.maxlayer;
      $("q-months").value = String(h.months);
      startQuery({ code: h.code, maxlayer: h.maxlayer });
    };
  });
}
function updateCrumb() {
  const parts = [MODES[S.mode].label];
  S.stack.forEach(s => parts.push(s.code));
  const cur = $("q-code").value.trim();
  if (cur && (!S.stack.length || S.stack[S.stack.length - 1].code !== cur)) parts.push(cur);
  $("crumb").textContent = parts.join("  ›  ");
}

/* =============================== 模式切换 & 事件 =============================== */
function switchMode(mode) {
  if (S.busy) { toast("查询进行中，请先停止", "err"); return; }
  const changed = S.mode !== mode;
  S.mode = mode;
  document.querySelectorAll(".rail-item").forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
  $("tb-onlychk-label").textContent = MODES[mode].onlyLabel;
  $("tb-onlychk-wrap").style.display = "";
  $("tb-miss-wrap").style.display = mode === "samconfirms" ? "" : "none";
  $("crumb").textContent = MODES[mode].label;
  $("tb-search").placeholder = "🔍 搜索所有列…";
  if (changed) {
    // 换模式就看旧数据没意义：清空结果，回到空态（避免拿着流程查询的数据看确认单列）
    S.data = null;
    S.view = { search: "", sortKey: "", sortDir: 1, filters: {}, onlyHas: mode === "rels", onlyMissing: false };
    $("tb-search").value = "";
    $("tb-onlychk").checked = mode === "rels";
    $("tb-miss").checked = false;
    $("result-toolbar").style.display = "none";
  }
  if (!S.data) {
    $("table-host").innerHTML = '<div class="empty"><div class="empty-icon">🔎</div>' +
      '<div class="empty-title">' + escapeHtml(MODES[mode].empty) + "</div>" +
      '<div class="empty-sub">输入物料编码后点「开始查询」；双击结果行可展开其下级 BOM</div></div>';
  } else {
    renderResult();
  }
}

function wireEvents() {
  document.querySelectorAll(".rail-item").forEach(b => b.onclick = () => switchMode(b.dataset.mode));
  $("btn-run").onclick = () => startQuery();
  $("btn-stop").onclick = stopQuery;
  $("btn-back").onclick = () => {
    if (!S.stack.length) return;
    const prev = S.stack.pop();
    $("q-code").value = prev.code;
    startQuery({ code: prev.code, maxlayer: prev.maxlayer, keepStack: true });
  };
  $("q-code").onkeydown = (e) => { if (e.key === "Enter") startQuery(); };
  $("q-code").oninput = updateCrumb;

  $("tb-search").oninput = () => { S.view.search = $("tb-search").value.trim(); renderResult(); };
  $("tb-onlychk").onchange = () => { S.view.onlyHas = $("tb-onlychk").checked; renderResult(); };
  $("tb-miss").onchange = () => { S.view.onlyMissing = $("tb-miss").checked; renderResult(); };
  $("tb-clear").onclick = () => {
    S.view.search = ""; S.view.filters = {}; S.view.sortKey = ""; S.view.onlyMissing = false;
    S.view.onlyHas = S.mode === "rels";
    $("tb-search").value = ""; $("tb-onlychk").checked = S.view.onlyHas; $("tb-miss").checked = false;
    renderResult();
  };
  $("tb-export").onclick = doExport;

  $("btn-clear-history").onclick = () => { S.history = []; try { localStorage.removeItem("pdmq.history"); } catch (_) {} renderHistory(); };

  // 登录
  $("login-pill").onclick = () => { loadStatus(); openLogin(); };
  $("btn-relogin").onclick = async () => {
    $("btn-relogin").disabled = true;
    const ok = await autoLogin(false);
    $("btn-relogin").disabled = false;
    if (ok) toast("已接管客户端登录态", "ok");
  };
  $("btn-login-more").onclick = openLogin;
  $("btn-quit").onclick = async () => {
    if (!confirm("退出 SinoCC PDM 查询工具？\n（后台服务会停止，下次启动重新自动登录）")) return;
    try { await api("POST", "/api/shutdown"); } catch (_) {}
    toast("程序已退出，请关闭本窗口");
    setTimeout(() => { try { window.close(); } catch (_) {} }, 600);
  };
  document.querySelectorAll("[data-login-tab]").forEach(b => b.onclick = () => {
    document.querySelectorAll("[data-login-tab]").forEach(x => x.classList.toggle("active", x === b));
    document.querySelectorAll("[data-login-body]").forEach(x => {
      x.style.display = x.dataset.loginBody === b.dataset.loginTab ? "" : "none";
    });
  });
  $("btn-auto-login").onclick = async () => {
    $("login-msg").className = "login-msg";
    $("login-msg").textContent = "正在扫描客户端登录态…";
    const ok = await autoLogin(false);
    $("login-msg").className = "login-msg " + (ok ? "ok" : "err");
    $("login-msg").textContent = ok ? "登录成功" : "未成功，请确认客户端已登录或改用其它方式";
    if (ok) setTimeout(() => closeOverlay("login-overlay"), 500);
  };
  $("btn-pwd-login").onclick = async () => {
    const account = $("in-account").value.trim(), password = $("in-password").value;
    if (!account || !password) { $("login-msg").className = "login-msg err"; $("login-msg").textContent = "请输入账号和密码"; return; }
    $("login-msg").className = "login-msg"; $("login-msg").textContent = "登录中…";
    const r = await api("POST", "/api/auth/login", { account, password });
    if (r.status === 200 && r.data && r.data.logged_in) {
      renderLogin(r.data);
      $("login-msg").className = "login-msg ok"; $("login-msg").textContent = "登录成功";
      setTimeout(() => closeOverlay("login-overlay"), 500);
    } else {
      $("login-msg").className = "login-msg err";
      $("login-msg").textContent = (r.data && r.data.detail) || ("登录失败 http " + r.status);
    }
  };
  $("btn-uid-login").onclick = async () => {
    const uid = $("in-uid").value.trim();
    if (!uid) { $("login-msg").className = "login-msg err"; $("login-msg").textContent = "请粘贴 loginguid"; return; }
    $("login-msg").className = "login-msg"; $("login-msg").textContent = "校验中…";
    const r = await api("POST", "/api/auth/session", { login_uid: uid });
    if (r.status === 200 && r.data && r.data.logged_in) {
      renderLogin(r.data);
      $("login-msg").className = "login-msg ok"; $("login-msg").textContent = "已注入登录态";
      setTimeout(() => closeOverlay("login-overlay"), 500);
    } else {
      $("login-msg").className = "login-msg err";
      $("login-msg").textContent = (r.data && r.data.detail) || ("注入失败 http " + r.status);
    }
  };

  // 关闭
  document.querySelectorAll("[data-close]").forEach(b => b.onclick = () => closeOverlay(b.dataset.close));
  document.querySelectorAll(".overlay").forEach(o => o.onclick = (e) => { if (e.target === o) o.style.display = "none"; });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#colmenu") && !e.target.closest("#rowmenu")) {
      $("colmenu").style.display = "none"; $("rowmenu").style.display = "none";
    }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("colmenu").style.display = "none"; $("rowmenu").style.display = "none"; } });
}

/* =============================== 启动 =============================== */
async function boot() {
  wireEvents();
  loadHistory();
  switchMode("rels");
  const r = await api("GET", "/api/meta");
  if (r.status === 200 && r.data) {
    $("app-ver").textContent = "v" + r.data.version;
    $("meta-line").textContent = "服务 " + API.replace(/^https?:\/\//, "") + "\nPDM " + (r.data.base_url || "").replace(/^https?:\/\//, "") +
      "\n组织 " + (r.data.entcode || "");
    $("meta-line").style.whiteSpace = "pre-line";
  }
  const st = await loadStatus();
  if (!st.logged_in) await autoLogin(true);
  updateCrumb();
}
boot();
