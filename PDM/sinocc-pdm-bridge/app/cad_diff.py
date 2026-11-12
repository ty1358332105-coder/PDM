# -*- coding: utf-8 -*-
r"""两版图纸结构化差异（增/删/改 × 图层 × 位置 × 旧值/新值）+ 更改标记层文本捷径。

数据源：cadcraft-cli `drawing.inspect {"entities": true}` 原生实体 dump
（app/cadcraft.cad_dump_entities，2026-10-15 实证：type/layer/handle/geometry 全量 JSON）。

匹配策略（启发式，按可信度递减）：
  1. 同 type+layer+几何签名（浮点规范化后精确相等） → 未变更（unchanged）
  2. 剩余实体在同 type+layer 桶内按质心就近配对（距离 ≤ tol 图纸单位） → modified
     （字段级 old/new：text / geometry.<路径> / position）
  3. 旧版剩余 = removed；新版剩余 = added
注意：多段线顶点增删会让字段级 diff 按索引错位膨胀，属已知口径（如实报告字段差异）。

「更改标记层文本清单」捷径：mark_layer（默认 "12更改标记"）先精确匹配图层名，
匹配不到退化为**包含**「更改标记」的图层；收集该层 Text/MText/Dimension 文本，
并标注 in_old（同层同文本是否在旧版已存在）= 新增标记一眼可见。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .cadcraft import cad_dump_entities

# 单类清单返回上限（防 MCP 响应爆炸；summary 计数不受影响）
MAX_ROWS = 500
MAX_CHANGES = 20

_TEXT_LIKE = {"Text", "MText", "Dimension", "Attribute"}


def _round(v: Any, nd: int = 3) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, dict):
        return {k: _round(x, nd) for k, x in v.items()}
    if isinstance(v, list):
        return [_round(x, nd) for x in v]
    return v


def _canon_geom(geom: Any) -> str:
    """几何签名：规范化浮点后序列化（键序无关）。"""
    return json.dumps(_round(geom), sort_keys=True, ensure_ascii=False)


def _flat(d: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flat(v, p))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(_flat(v, f"{prefix}[{i}]"))
    else:
        out[prefix or "value"] = d
    return out


def _find_pt(obj: Any) -> Optional[Tuple[float, float]]:
    """递归找第一个 {x,y} 结构（position 兜底）。"""
    if isinstance(obj, dict):
        if "x" in obj and "y" in obj:
            try:
                return (float(obj["x"]), float(obj["y"]))
            except (TypeError, ValueError):
                return None
        for v in obj.values():
            r = _find_pt(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_pt(v)
            if r:
                return r
    return None


def _centroid(e: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """实体代表点：按类型取语义点，兜底递归找第一个坐标。"""
    g = e.get("geometry") or {}
    gt = str(g.get("type") or "")
    try:
        if gt == "line":
            a, b = g["a"], g["b"]
            return ((float(a["x"]) + float(b["x"])) / 2, (float(a["y"]) + float(b["y"])) / 2)
        if gt in ("circle", "arc"):
            c = g["center"]
            return (float(c["x"]), float(c["y"]))
        if gt == "lwPolyline":
            vs = g.get("vertices") or []
            if vs:
                return (sum(float(v["p"]["x"]) for v in vs) / len(vs),
                        sum(float(v["p"]["y"]) for v in vs) / len(vs))
        if gt == "hatch":
            loops = g.get("loops") or []
            if loops:
                vs = loops[0].get("vertices") or []
                if vs:
                    return (sum(float(v["p"]["x"]) for v in vs) / len(vs),
                            sum(float(v["p"]["y"]) for v in vs) / len(vs))
        if gt == "dimension":
            c = g.get("defpt")
            if c:
                return (float(c["x"]), float(c["y"]))
        if gt in ("text", "mText"):
            c = g.get("insert")
            if c:
                return (float(c["x"]), float(c["y"]))
    except (KeyError, TypeError, ValueError):
        pass
    return _find_pt(g)


def _norm_text(s: Any) -> str:
    """文本归一化：MText 换行 \\P → 空格，去格式码与多余空白。"""
    if not isinstance(s, str):
        return ""
    s = re.sub(r"\\[Pp]", " ", s)
    s = re.sub(r"\\[A-Za-z][^;\\]*;?", "", s)
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


def _text_of(e: Dict[str, Any]) -> str:
    g = e.get("geometry") or {}
    return _norm_text(g.get("value") if g.get("type") == "text" else g.get("contents"))


def _row(e: Dict[str, Any], pos: Optional[Tuple[float, float]],
         changes: Optional[List[Dict[str, Any]]] = None,
         handle2: str = "") -> Dict[str, Any]:
    r: Dict[str, Any] = {"type": e.get("type"), "layer": e.get("layer"),
                         "handle": e.get("handle")}
    if handle2:
        r["handle_new"] = handle2
    if pos:
        r["position"] = [round(pos[0], 3), round(pos[1], 3)]
    t = _text_of(e)
    if t:
        r["text"] = t
    if changes:
        r["changes"] = changes
    return r


def _mark_layer_of(layers: List[Dict[str, Any]], mark: str) -> Tuple[Optional[str], str]:
    """解析更改标记层：先精确名，退化含「更改标记」。返回 (层名或 None, 模式)。"""
    names = [str(l.get("name") or "") for l in (layers or [])]
    if mark in names:
        return mark, "exact"
    for n in names:
        if "更改标记" in n:
            return n, "fuzzy"
    return None, "none"


async def cad_diff(path_a: str, path_b: str, tol: float = 1.0,
                   mark_layer: str = "12更改标记") -> Dict[str, Any]:
    """旧版 A vs 新版 B → 增/删/改结构化清单 + 更改标记层文本捷径。"""
    if not isinstance(tol, (int, float)) or tol <= 0:
        return {"ok": False, "error": "tol 必须是正数（图纸单位）"}
    da, db = await cad_dump_entities(path_a), await cad_dump_entities(path_b)
    if not da.get("ok"):
        return {"ok": False, "error": f"旧版读取失败: {da.get('error')}"}
    if not db.get("ok"):
        return {"ok": False, "error": f"新版读取失败: {db.get('error')}"}
    ea: List[Dict[str, Any]] = da["entities"]
    eb: List[Dict[str, Any]] = db["entities"]

    def key(e: Dict[str, Any]) -> Tuple[str, str]:
        return (str(e.get("type") or "?"), str(e.get("layer") or "?"))

    # ① 精确签名配对 → unchanged
    sig_a: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for e in ea:
        sig_a[(key(e), _canon_geom(e.get("geometry")))] = e
    unchanged = 0
    rest_b: List[Dict[str, Any]] = []
    for e in eb:
        k = (key(e), _canon_geom(e.get("geometry")))
        if k in sig_a:
            sig_a.pop(k)
            unchanged += 1
        else:
            rest_b.append(e)
    rest_a: List[Dict[str, Any]] = list(sig_a.values())

    # ② 桶内就近配对 → modified；③ 剩余 → removed / added
    buckets_a: Dict[Tuple[str, str], List[Tuple[Dict[str, Any], Tuple[float, float]]]] = {}
    for e in rest_a:
        c = _centroid(e)
        if c:
            buckets_a.setdefault(key(e), []).append((e, c))
    modified: List[Dict[str, Any]] = []
    used_b: set = set()
    for e_b in rest_b:
        c_b = _centroid(e_b)
        if not c_b:
            continue
        cands = buckets_a.get(key(e_b)) or []
        best, best_d = None, tol
        for e_a, c_a in cands:
            d = ((c_a[0] - c_b[0]) ** 2 + (c_a[1] - c_b[1]) ** 2) ** 0.5
            if d <= best_d:
                best, best_d = (e_a, c_a), d
        if best is None:
            continue
        e_a = best[0]
        cands.remove(best)
        used_b.add(id(e_b))
        fa, fb = _flat(_round(e_a.get("geometry"))), _flat(_round(e_b.get("geometry")))
        changes = [{"field": k, "old": fa[k], "new": fb[k]}
                   for k in sorted(set(fa) | set(fb))
                   if fa.get(k) != fb.get(k)][:MAX_CHANGES]
        modified.append(_row(e_b, c_b, changes, handle2=str(e_a.get("handle") or "")))
    added = [_row(e, _centroid(e)) for e in rest_b if id(e) not in used_b]
    removed = [_row(e, _centroid(e)) for e, _c in
               [(_e, _c) for _bkt in buckets_a.values() for _e, _c in _bkt]]

    # 更改标记层文本捷径（以新版为准，标注旧版是否已存在）
    ml, mode = _mark_layer_of(db.get("layers"), mark_layer)
    old_texts = {(_norm_text_key(e), str(e.get("layer") or ""))
                 for e in ea}
    mark_items: List[Dict[str, Any]] = []
    if ml:
        for e in eb:
            if str(e.get("layer") or "") != ml:
                continue
            t = _text_of(e)
            if not t:
                continue
            mark_items.append({"handle": e.get("handle"), "type": e.get("type"),
                               "text": t, "position": list(_centroid(e) or ()),
                               "in_old": (_norm_text_key(e), str(e.get("layer") or "")) in old_texts})
    mark_items.sort(key=lambda x: (x.get("in_old") is True, str(x.get("text"))))

    return {
        "ok": True,
        "path_a": str(path_a), "path_b": str(path_b),
        "tol": float(tol),
        "summary": {"added": len(added), "removed": len(removed),
                    "modified": len(modified), "unchanged": unchanged,
                    "total_a": len(ea), "total_b": len(eb)},
        "added": added[:MAX_ROWS], "removed": removed[:MAX_ROWS],
        "modified": modified[:MAX_ROWS],
        "rows_capped": max(len(added), len(removed), len(modified)) > MAX_ROWS,
        "mark_texts": {"layer": ml, "match_mode": mode,
                       "total": len(mark_items), "items": mark_items[:MAX_ROWS]},
        "note": "匹配为启发式：签名一致=未变更；同型同层就近=变更；其余=增删。"
                "多段线顶点增删会让字段 diff 按索引展开，属已知口径。",
    }


def _norm_text_key(e: Dict[str, Any]) -> str:
    return _text_of(e).casefold()
