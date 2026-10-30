# -*- coding: utf-8 -*-
"""板块信息面板 + 个股操作建议（2026-10-23 16:39 需求）。

四个数据块，能力边界先写清楚，防止前端把近似值当全量：

1. 归属索引（这只票属于哪些板块）：新浪板块没有「按股查板块」的接口，
   只能把全部板块的成分股拉一遍反查。255 类 × 分页 ≈ 500 个请求、约 2 分钟，
   所以做成后台预热 + 当日缓存（state/board_index.json），索引没就绪时明说。
2. 板块资金流向：新浪板块**没有净流入字段**。用样本池（成交额前 N + 自选）
   已采到的四档资金按成员聚合，标「样本口径」并给覆盖率 —— 覆盖率 <30% 时
   该块直接显示数据不足，不冒充全量。
3. 梯队与强度：成员按涨幅排。涨停判定优先用快照的真实涨停价（is_limit_up），
   不在快照里的成员按 pc≥9.8 粗判并单独计数。强度给三个可解释分量
   （板块涨幅 / 涨停家数 / 样本净流入），各带阈值定性，**不合成假分数**。
4. 持续性：需要板块日 K。东财 BK 日线间歇可用（且只有 src=em 的板块有 BK 码），
   新浪板块当下拿不到 —— 返回 na 与原因，不编。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from ..collector import em, kline, sina
from . import picker as picker_mod

INDEX_PATH = Path("state/board_index.json")
INDEX_TTL_SEC = 20 * 3600          # 归属索引当天有效；次日首查后台重建
PANEL_TTL = 60.0                   # 板块面板缓存
PANEL_MAX_MEMBERS = 300            # 梯队统计窗口（成分股按涨幅取前 300）

_index: dict[str, list[dict]] = {}      # code -> [{c,n,k,pct}]
_index_meta: dict = {"date": "", "done": False, "n_boards": 0, "done_n": 0,
                     "building": False, "built_at": ""}
_panel_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


# ── 归属索引 ────────────────────────────────────────────────
def _load_index_if_fresh() -> bool:
    """今天已经建过 → 直接读盘。归属关系日内几乎不变，没必要重复 2 分钟的活。

    **空索引一律当作没建过**。2026-10-26 踩的坑：源派发写错 → 1033 类全部查空 →
    空结果照样落盘，之后每天都判定「今天建过了」，归属面板从此永远是空的，
    而且看日志一切正常（done_n 还一路涨到 1033）。
    """
    if _index and _index_meta.get("date") == date.today().isoformat():
        return True
    try:
        if INDEX_PATH.exists():
            j = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            mp = j.get("map") or {}
            if j.get("date") == date.today().isoformat() and mp:
                with _lock:
                    _index.update(j.get("map") or {})
                    _index_meta.update({**j.get("meta", {}), "done": True,
                                        "building": False})
                return True
    except Exception:                          # noqa: BLE001 — 缓存坏了就重建
        pass
    return False


def _save_index() -> None:
    """落盘当日缓存。空索引不写——把「一只都没查到」写进去，等于把故障固化成
    「今天已建过」，下次启动就不会再重建了。"""
    if not _index:
        return
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(json.dumps(
            {"date": _index_meta["date"], "map": _index,
             "meta": {k: v for k, v in _index_meta.items() if k != "building"}},
            ensure_ascii=False), encoding="utf-8")
    except Exception:                          # noqa: BLE001
        pass


def build_index(boards: list, members_fetch=None) -> dict:
    """全量反查：拉每一类的成分股，反向登记 code → 板块。

    boards: store.snapshot_boards() 的结果（Board dataclass 列表）。
    同步函数，调用方负责丢进 to_thread / 后台任务。
    """
    members_fetch = members_fetch or _members_of
    with _lock:
        if _index_meta.get("building"):
            return dict(_index_meta)
        _index_meta["building"] = True
        _index_meta["done"] = False
    new_map: dict[str, list[dict]] = {}
    done = 0
    try:
        for b in boards:
            try:
                rows, _total = members_fetch(b.code)
            except Exception:                  # noqa: BLE001 — 单类失败不拖垮整体
                rows = []
            for r in rows:
                c = r.get("c") or ""
                if not c:
                    continue
                new_map.setdefault(c, []).append(
                    {"c": b.code, "n": b.name, "k": b.kind, "pct": b.pct})
            done += 1
            with _lock:
                _index_meta["done_n"] = done
        with _lock:
            _index.clear()
            _index.update(new_map)
            _index_meta.update({"date": date.today().isoformat(), "done": True,
                                "n_boards": done, "built_at": time.strftime("%H:%M"),
                                "building": False})
        _save_index()
    finally:
        with _lock:
            _index_meta["building"] = False
    return dict(_index_meta)


def _members_of(bk: str, max_members: int = 400) -> tuple[list[dict], int]:
    """拉一类板块的成员（翻页到 max_members 或取完）。

    板块码前缀决定走哪个源：**BK 开头是东财板块码，其余是新浪节点**。
    2026-10-26 修：这里原来无脑走新浪。东财 clist 被封期间板块源本来就是新浪，
    看不出问题；东财一恢复（板块源变 em、码是 BKxxxx），拿新浪接口去查东财码
    全部返回空——1033 类"构建完成"、done_n 一路涨到 1033，但 _index 一只票都没有，
    抽屉里的「板块」归属永远是空。派发逻辑与下面的 _fetch_members 保持一致。
    """
    out: list[dict] = []
    total = 0
    page = 1
    while len(out) < max_members and page <= 6:
        if bk.upper().startswith("BK"):
            rows, total = em.fetch_board_members(bk, page=page, size=100)
        else:
            rows, total = sina.fetch_board_members(bk, page=page, size=100)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    return out, total


def index_status() -> dict:
    with _lock:
        return {**_index_meta, "stocks": len(_index)}


def ensure_index(store) -> dict:
    """给接口用：新鲜就返回状态；过期就后台重建（首次请求立刻有反馈，不阻塞）。"""
    if _load_index_if_fresh():
        return index_status()
    if not _index_meta.get("building"):
        boards = store.snapshot_boards()
        if boards:
            import asyncio
            asyncio.get_running_loop().run_in_executor(
                None, lambda: build_index(boards))
    return index_status()


def boards_of(store, code: str) -> tuple[list[dict], dict]:
    ensure_index(store)
    with _lock:
        bl = list(_index.get(code, []))
    # index_status 也要抢同一把非重入锁 —— 不能在锁内调，否则自死锁
    # （2026-10-23 faulthandler 转储定位：挂死在 index_status 抢锁）
    return bl, index_status()


# ── 板块面板 ────────────────────────────────────────────────
def _fetch_members(bk: str) -> list[dict]:
    """成员（按涨幅降序，新浪接口本身就是降序）。BK 开头走东财。"""
    out: list[dict] = []
    page = 1
    while len(out) < PANEL_MAX_MEMBERS and page <= 4:
        if bk.upper().startswith("BK"):
            rows, _t = em.fetch_board_members(bk, page=page, size=100)
        else:
            rows, _t = sina.fetch_board_members(bk, page=page, size=100)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    return out[:PANEL_MAX_MEMBERS]


def _persistence(bk: str) -> dict:
    """持续性：只有东财 BK 码能拿板块日线。新浪板块如实返回不可得。"""
    if not bk.upper().startswith("BK"):
        return {"ok": False,
                "note": "新浪兜底板块没有板块代码/日线，持续性暂算不了"
                        "（东财口径恢复后本板块会自动可用）"}
    try:
        j = kline.fetch(bk, "day", n=15)
        bars = j.get("bars") or []
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "note": f"板块日K取不到: {e}"[:80]}
    if len(bars) < 6:
        return {"ok": False, "note": "板块日K根数不足"}
    cl = [b["c"] for b in bars]
    chg5 = (cl[-1] / cl[-6] - 1) * 100
    yang = sum(1 for b in bars[-5:] if b["c"] >= b["o"])
    ma5 = sum(cl[-5:]) / 5
    if chg5 > 2 and yang >= 3:
        label = "延续"
    elif chg5 < -2 or (yang <= 1):
        label = "退潮"
    else:
        label = "反复"
    return {"ok": True, "chg5": round(chg5, 2), "yang": yang,
            "above_ma5": cl[-1] >= ma5, "label": label}


def panel(store, bk: str) -> dict:
    now = time.monotonic()
    hit = _panel_cache.get(bk)
    if hit and now - hit[0] < PANEL_TTL:
        return hit[1]

    b = store.board(bk)
    members = _fetch_members(bk)
    if not members:
        out = {"ok": False, "error": "成分股取不到（源站被限或板块码无效）",
               "bk": bk}
        _panel_cache[bk] = (now, out)
        return out

    # 用快照补真实涨停判定 + 资金流
    zt: list[dict] = []
    rough_zt = 0
    tiers = {"zt": 0, "up5": 0, "up0": 0, "dn": 0}
    net_sum = 0.0
    net_hit = 0
    contrib: list[dict] = []
    for r in members:
        q = store.get(r["c"])
        f = store.flow_of(r["c"])
        is_zt = bool(q.is_limit_up) if q is not None else r["pc"] >= 9.8
        rough = q is None and r["pc"] >= 9.8
        if is_zt:
            tiers["zt"] += 1
            if rough:
                rough_zt += 1
            zt.append({"c": r["c"], "n": r["n"], "pc": r["pc"]})
        elif r["pc"] >= 5:
            tiers["up5"] += 1
        elif r["pc"] >= 0:
            tiers["up0"] += 1
        else:
            tiers["dn"] += 1
        if f:
            net_hit += 1
            net_sum += f.get("net", 0.0)
            contrib.append({"c": r["c"], "n": r["n"], "net": f.get("net", 0.0),
                            "pc": r["pc"]})

    total = len(members)
    contrib.sort(key=lambda x: x["net"], reverse=True)
    pct_b = b.pct if b else (sum(r["pc"] for r in members) / max(1, total))

    # 强度：三个可解释分量，阈值写在返回里，前端直接展示
    s_pct = "强" if pct_b >= 2 else ("中" if pct_b >= 0.5 else "弱")
    s_zt = "强" if tiers["zt"] >= 3 else ("中" if tiers["zt"] >= 1 else "弱")
    s_net = ("数据不足" if net_hit < max(3, int(total * 0.3))
             else ("强" if net_sum > 1 else ("中" if net_sum >= 0 else "弱")))
    strong = (s_pct, s_zt, s_net).count("强")
    weak = (s_pct, s_zt, s_net).count("弱")
    label = "强" if strong >= 2 else ("弱" if weak >= 2 else "中")

    out = {
        "ok": True, "bk": bk,
        "bn": b.name if b else bk, "pct": round(pct_b, 2),
        "src": b.src if b else "sina",
        "total": total, "truncated": total >= PANEL_MAX_MEMBERS,
        "ladder": {**tiers, "rough_zt": rough_zt},
        "zt": zt[:10],
        "top": [{"c": r["c"], "n": r["n"], "p": r["p"], "pc": r["pc"]}
                for r in members[:10]],
        "flow": {"hit": net_hit, "cov": round(net_hit / max(1, total) * 100),
                 "net": round(net_sum, 2),
                 "top": contrib[:5],
                 "note": "样本口径：只统计已采到四档资金的成员，不是全量"},
        "strength": {
            "label": label,
            "items": [
                {"k": "板块涨幅", "v": f"{pct_b:+.2f}%", "judge": s_pct,
                 "rule": "≥2%强 / 0.5~2%中 / <0.5%弱"},
                {"k": "涨停家数", "v": str(tiers["zt"]), "judge": s_zt,
                 "rule": "≥3家强 / 1~2家中 / 0弱"},
                {"k": "样本净流入", "v": f"{net_sum:+.2f}亿", "judge": s_net,
                 "rule": ">1亿强 / 0~1亿中 / <0弱（覆盖率"
                         f"{round(net_hit / max(1, total) * 100)}%）"},
            ],
        },
        "persist": _persistence(bk),
    }
    _panel_cache[bk] = (now, out)
    return out


# ── 个股操作建议 ────────────────────────────────────────────
def _swing_levels(bars: list[dict], price: float) -> tuple[float | None, float | None]:
    """日线摆动点取最近的 R1 / S1（与前端 kLevels 同思路，只取最近一条）。"""
    if len(bars) < 10:
        return None, None
    k = 2
    his, los = [], []
    for i in range(k, len(bars) - k):
        seg = bars[i - k:i + k + 1]
        if all(b["h"] <= bars[i]["h"] for b in seg if b is not bars[i]):
            his.append(bars[i]["h"])
        if all(b["l"] >= bars[i]["l"] for b in seg if b is not bars[i]):
            los.append(bars[i]["l"])
    r1 = min((h for h in his if h > price * 1.002), default=None)
    s1 = max((v for v in los if v < price * 0.998), default=None)
    return r1, s1


def advice(store, code: str) -> dict:
    """单票过一遍六模式 + 打分 + 给参与/观察/回避。

    判定函数与选股面板**完全同一套**（_gate_a / _judge_b / _score），
    这里只做组装 —— 两套标准迟早给出互相矛盾的建议。
    """
    q = store.get(code)
    if q is None:
        return {"ok": False, "error": "快照里没有这只票"}

    bars: list[dict] = []
    modes_out: list[dict] = []
    hits: list[str] = []
    for m in picker_mod.MODES:
        mk, name = m["k"], m["n"]
        ok, why = picker_mod._gate_a(q, mk, m["params"])
        if not ok:
            modes_out.append({"k": mk, "n": name, "lv": m["lv"],
                              "hit": False, "why": why})
            continue
        if m["lv"] == "A":
            modes_out.append({"k": mk, "n": name, "lv": "A",
                              "hit": True, "why": why})
            hits.append(mk)
            continue
        if not bars:                       # 惰性：有多只 B 模式也只拉一次分时
            bars = picker_mod._bars(code)
        ok_b, why_b = picker_mod._judge_b(q, mk, m["params"], bars)
        modes_out.append({"k": mk, "n": name, "lv": "B",
                          "hit": ok_b, "why": why_b})
        if ok_b:
            hits.append(mk)

    score = picker_mod._score(q, hits)
    vwap = round(picker_mod._avg_price(q), 2)
    r1, s1 = None, None
    try:
        j = kline.fetch(code, "day", n=160)
        r1, s1 = _swing_levels(j.get("bars") or [], q.price)
    except Exception:                      # noqa: BLE001 — 关键位取不到不挡建议
        pass

    if score >= 75 and hits:
        verdict, tone = "可参与", "go"
        act = (f"命中 {' + '.join(hits)}，按所选模式的介入逻辑执行；"
               f"短线参考：分时均价 {vwap} 上方持有")
    elif score >= 60 or hits:
        verdict, tone = "观察", "watch"
        act = "有模式苗头但强度不够，进自选盯，不先动手"
    else:
        verdict, tone = "回避", "no"
        act = "当前不匹配任何进攻模式，右侧体系不介入"

    notes = []
    if q.pct < 0:
        notes.append("当日下跌，右侧体系原则上不接飞刀")
    if not bars:
        notes.append("分时未取到（源站间歇封锁），B 层模式本轮未参与判定，"
                     "结论偏保守")

    stops = []
    if q.open > 0:
        stops.append(f"跌破开盘价 {q.open:.2f} 说明开盘突破失效")
    if s1:
        stops.append(f"跌破支撑 S1 {s1:.2f} 离场")
    if vwap > 0:
        stops.append(f"跌破分时均价 {vwap} 减仓")

    return {"ok": True, "c": code, "n": q.name, "pct": q.pct, "score": score,
            "verdict": verdict, "tone": tone, "act": act,
            "modes": modes_out, "hits": hits,
            "vwap": vwap, "r1": r1, "s1": s1,
            "stops": stops, "notes": notes}
