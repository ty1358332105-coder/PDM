# SinoCC PDM 查询工具（独立版）

把「SinoCC PDM 桥接」里的 **流程查询** 与 **确认单查询** 单独抽出来做成的只读查询小软件：
自动登录、查询口径与原桥接完全一致、长查询有确定性进度条、整体式单窗口界面、可导出 Excel。

```
┌─────────────────────────────────────────────────────────────────────┐
│ PDM 查询工具 v1.0     流程查询 › 814042400004     ● 唐阳 · CHIGOCAC   │  ← 顶栏（登录态）
├──────────────┬──────────────────────────────────────────────────────┤
│ 流程查询      │ 物料编码[________] 层级[5 层] 时间窗[6 月] [开始查询]  │
│ 确认单查询    ├──────────────────────────────────────────────────────┤
│              │ ① 读 BOM 明细 ✓  ② 扫物料确认单 ●  ③ 读确认单明细       │  ← 进度条（分段 + 计数 + 剩余时间）
│ 最近查询      │ ████████████░░░░░░░░░░  35%   204/250  剩余 98.6s      │
│ …            ├──────────────────────────────────────────────────────┤
│              │ 层级 物料编码 中文名称 临时确认（TC） 正式确认（SC） …  │  ← 结果表（排序/筛选/搜索/导出）
└──────────────┴──────────────────────────────────────────────────────┘
```

## 功能

| 项 | 说明 |
|---|---|
| 流程查询 | 整棵 BOM 多层明细 + 每行物料**在途**（流程 stat 2~6）的样品确认单 / 变更流程单；24 列，点单号看详情 |
| 确认单查询 | 只看**采购件（采购标识 P / OP）**，逐行列出该物料**全部**确认单：TC 临时 / SC 正式**分两列**，同一物料多张堆在同一格，每张显示「编码 + 供应商全称 + 简称」；**一张确认单都没有的行整行标红**（含「无确认单」标签），可勾「只看缺确认」直接出跟催清单 |
| 时间窗 | 流程查询：物料受控时间 + 单据编制时间（同桥接口径）。确认单查询：**只按「物料受控时间」卡窗口**（窗外的物料整行不出现、也不计入缺单统计），窗内物料的确认单**不限编制时间全部列出** |
| 层级 | 1 / 2 / 3 / 5 / 全部 |
| 交互 | 表头点击排序、表头右键筛选（含「有/无」两档）、全局搜索、只看有单据 / 只看缺确认、双击行下钻下级 BOM、行右键（展开 BOM / 物料属性 / 复制编码） |
| 导出 | SpreadsheetML（.xls，无需装 Office 组件），多张确认单在单元格内换行堆叠；确认单查询多一列「确认单（有 N / 无）」 |
| 登录 | 自动：扫本机 SinoCC 客户端进程内存拿 loginguid（免密码）；兜底：密码登录 / 粘贴 loginguid |
| 停止 | 查询途中可秒停（worker task 直接 cancel） |

## 启动

```bat
:: 方式一：双击 start.bat（优先用项目内 .venv，其次用同级桥接的 .venv，最后退到系统 python）
:: 方式二：命令行
python run.py                 :: 起服务(9130) + 打开整体式窗口（Chrome/Edge --app 无地址栏窗口）
python run.py --browser       :: 强制用浏览器 --app 窗口（不用 pywebview）
python run.py --no-window     :: 只起服务，自己用浏览器打开 http://127.0.0.1:9130/
python run.py --port 9131     :: 换端口
```

装了 `pywebview` 时会开原生窗口（最像桌面软件）；没装就用 Chrome/Edge 的 `--app=` 无边框窗口，
两者都是「整体式」——没有浏览器地址栏/标签页，标题栏就是软件名。

> 端口默认 **9130**，与桥接的 9123 / 源码态 9125 互不冲突；端口已被本程序占用时只开窗口不重复起服务。

## 目录

```
run.py                 启动器（单实例 + 窗口）
pdmq/
  __init__.py          create_app：装配路由、静态页、/api/health、/api/meta
  config.py            Settings（SINOCC_ 前缀环境变量 / .env）
  auth.py              自动登录 / 密码登录 / 粘贴 loginguid / 状态
  jobs.py              任务与分段进度（进度条数据源）
  query.py             流程查询 + 确认单查询（两个 job，带进度回调）
  objects.py           确认单 / 变更单 / 物料 只读详情
  static/              index.html + app.css + app.js（整体式界面）
  vendor/              协议层（从 sinocc-pdm-bridge/app 同步：client / client_zipxml /
                       bom_templates / doc_templates / wf_templates / auto_login）
tools/
  cdp.py               极简 CDP 脚手架
  ui_probe_query_app.py 全链路 UI 回归（21 项断言 + 截图）
docs/ui/               探针报告与截图
```

`pdmq/vendor/` 是从桥接复制过来的**已抓包实证**协议层，改动原则：**协议细节只以抓包为准**，
同步方式为「复制 + 把 `from app.xxx` 改成相对导入」，不要在这里另写一套猜测实现。

## 与桥接的口径一致性（实测对照）

> 说明：**流程查询**与桥接逐条一致；**确认单查询** 2026-11-09 起按用户要求收窄为
> 「只看采购件 P/OP + 时间窗按物料受控时间 + 无确认单标红」，因此行数与桥接那份（全部物料）不同。

同一物料 `814042400004`、2 层，两个程序结果逐项比对：

| 查询 | 桥接 | 本工具 | 结论 |
|---|---|---|---|
| 流程查询（2 层 / 12 月） | rows 284，在途 9，超期 203/29/8314，对象 151 | rows 284，在途 9，超期 203/29/8314，对象 151 | **完全一致**（active_rels 逐条 objcode/type/stat 相同） |
| 确认单查询（2 层 / 不限，收窄前） | 153 物料 / 319 张（TC 52 + SC 267） | 153 物料 / 319 张 | **完全一致**（code/kind/vendor 三元组逐条相同） |
| 确认单查询（2 层 / 12 月，现口径） | — | 45 行 / 43 个在窗采购件；80 张确认单；**7 行无确认单**（标红）；受控超窗 163 个已排除 | 用户定案口径 |

时间窗口径的两个实测锚点（都踩过坑，已固化成冒烟用例）：

- `801242001527` 受控 2019-06、单号 2019/2022 → **窗 36 月也整行不出现**，不计入缺单；
- `802103100075` 受控 2025-05、确认单 2025-05~07 → **算「有 3 张」**，不再误报无确认。

性能（同一台机器、冷缓存）：确认单查询 2 层 284 行 ≈ 2 分 20 秒；热缓存（5/15 分钟 TTL 内）≈ 1 秒。

## HTTP 接口

```
GET  /api/health | /api/meta
GET  /api/auth/status
POST /api/auth/auto        // 自动登录（扫客户端进程）
POST /api/auth/login       // {account,password}
POST /api/auth/session     // {login_uid}
POST /api/auth/logout
POST /api/query/rels       // {itemcode,maxlayer,months} → {job_id}
POST /api/query/samconfirms
GET  /api/jobs/{id}        // 进度 {status,stage,stage_index,done,total,pct,message,log,elapsed_ms}
GET  /api/jobs/{id}/result // 结果（完成后）
POST /api/jobs/{id}/cancel
GET  /api/objects/{objid}/samconfirm | /ecn
GET  /api/items/{itemcode}
```

接口文档：<http://127.0.0.1:9130/api/docs>

## 回归

```bat
python tools/ui_probe_query_app.py --port 9130 --report docs/ui/report.txt
```

真实浏览器点真实按钮：钩子自证 → 自动登录 → 确认单查询（进度采样必须单调递增且有中间态）→
TC/SC 两列 + 供应商 → 点单号弹详情（含「供应商」）→ 切流程查询（24 列）→ 导出 toast → 无 console 错误。
当前 **21/21 通过**。

## 打包（2026-11-09 已完成）

产物：**`dist/SinoCC-PDM-Query.exe`（29.1 MB，单文件、无黑框、带图标）**

```bat
:: 重打包（不要用 PowerShell 管道，PyInstaller 会在 ~117s 处静默中断）
.venv\Scripts\python.exe tools\embed_static.py
.venv\Scripts\python.exe -m PyInstaller SinoCC-PDM-Query.spec ^
    --distpath dist --workpath build --noconfirm
```

三个关键点：

1. **前端资源内嵌**：`tools/embed_static.py` 把 `pdmq/static/*` 转成 `pdmq/assets_gen.py`（base64），
   打包后走内嵌通道（`/api/health` 里 `assets=embed`）。因为本机 onefile 的 `%TEMP%\_MEI*`
   会被企业终端 agent 在约 10 秒后清空（桥接项目 2026-09-21 复盘过），从磁盘读 index.html 会白屏。
2. **exe 名不能命中客户端扫描规则**：`sinocc*` 前缀会被 `auto_login` 当成 PDM 客户端扫，
   已把 `sinocc-pdm-query.exe / pdm-query.exe` 加进 `SELF_EXE_NAMES`，并用 `"query"` 标记兜底。
3. **窗口版（console=False）**：`sys.stdout/stderr` 为 None，`run.py` 里 `_fix_stdio()` 兜了黑洞 IO，
   日志改落 `%LOCALAPPDATA%\SinoCC-PDM-Query\logs\app.log`。

环境变量开关：`PDMQ_CONSOLE=1` 打带控制台的排障版；`PDMQ_ONEDIR=1` 打文件夹版。

冒烟（黑盒 HTTP，16 项）：`python tools/smoke_dist.py --exe dist/SinoCC-PDM-Query.exe --port 9134`
→ frozen/embed 自证、首页与静态资源、自动登录（且扫描列表不含本 exe）、确认单查询带供应商、
对象详情按 objtype 路由（91869 走确认单必须 404）、日志落盘。当前 **16/16 通过**。

