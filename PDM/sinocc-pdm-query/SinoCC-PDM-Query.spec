# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — SinoCC PDM 查询工具（默认单文件 onefile）。

打包命令（**不要接管道**，PS 管 PowerShell 管道会在 ~117s 处静默中断）：
    .venv\\Scripts\\python.exe -m PyInstaller SinoCC-PDM-Query.spec ^
        --distpath dist --workpath build --noconfirm

产物：dist/SinoCC-PDM-Query.exe（窗口版，无黑框；日志落
%LOCALAPPDATA%\\SinoCC-PDM-Query\\logs\\app.log）

两种形态：
  PDMQ_CONSOLE=1 → 带控制台（排障用）
  PDMQ_ONEDIR=1  → 文件夹形态（免得被安全软件拦 onefile 解包）

前端资源：pdmq/assets_gen.py（tools/embed_static.py 生成）已内嵌进 pyz，
所以即使 onefile 的 _MEI* 解包目录被企业终端 agent 清空，UI 也不会白屏；
datas 里仍带一份 pdmq/static，作为内嵌缺失时的兜底。
"""
import os

from PyInstaller.utils.hooks import collect_submodules, collect_all

CONSOLE = os.environ.get("PDMQ_CONSOLE", "0") == "1"
ONEDIR = os.environ.get("PDMQ_ONEDIR", "0") == "1"
APP_NAME = "SinoCC-PDM-Query" + ("-Console" if CONSOLE else "")

hiddenimports = (
    collect_submodules("uvicorn")
    + [
        "anyio._backends._asyncio",
        "pdmq.assets_gen",
        "pdmq.vendor.client_zipxml",
        "pdmq.vendor.auto_login",
        "pdmq.vendor.bom_templates",
        "pdmq.vendor.doc_templates",
        "pdmq.vendor.wf_templates",
        "dotenv",
    ]
)

# ---- pywebview（2026-11-09：捆进 exe，目标机不再依赖 Chrome/Edge 找窗口）----
# pywebview 在 Windows 走 .NET(WinForms) + WebView2；要把 pythonnet/clr_loader 的
# 程序集与 WebView2Loader.dll 一并收进来，用 collect_all 最稳。
for _pkg in ("webview", "pythonnet", "clr_loader", "clr"):
    try:
        _d, _b, _h = collect_all(_pkg)
        globals().setdefault("_extra_datas", []).extend(_d)
        globals().setdefault("_extra_binaries", []).extend(_b)
        hiddenimports = list(hiddenimports) + [h for h in _h if h not in hiddenimports]
    except Exception:
        pass
EXTRA_DATAS = globals().get("_extra_datas", [])
EXTRA_BINARIES = globals().get("_extra_binaries", [])

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=EXTRA_BINARIES,
    datas=[
        ("pdmq/static", "pdmq/static"),   # 兜底（正常走内嵌）
    ] + EXTRA_DATAS,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "pytest",
        "mcp",
        "scapy",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

_common = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/PDM.ico",
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **_common)
