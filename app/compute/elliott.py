# -*- coding: utf-8 -*-
"""波浪理论（Elliott Wave）：ZigZag 摆动点 → 浪型识别 → 斐波那契位。

## 先说清楚这个模块**不是**什么

波浪数法是**事后解释性**框架，不是预测器。同一段行情常常存在多种都"合规"的
数法，历史上用它亏钱的人比赚钱的多。所以这里有四条硬纪律，写进代码而不是写在
文档里：

1. **摆动点客观**：ZigZag 的反转阈值是参数（`pct`）+ 最小间隔（`min_gap`），
   同一组参数下摆动点是确定的，不做"我觉得这里像个底"的人工调整。
2. **浪型必须过规则才敢标**：推动浪必须同时满足艾略特的三条硬规则
   （2 浪不回破 1 浪起点 / 3 浪不是最短 / 5 浪创新高；4 浪不进 1 浪区间是软规则，
   违反时记为倾斜三角并扣分）。任一条硬规则不满足 → `pattern=None`，**绝不硬套**。
3. **斐波那契位是数学，目标是概率**：回撤比例与扩展倍数是精确算出来的；
   但"会不会走到"没有保证，所以每个目标位都同时给**失效位**（结构被破坏的价格）。
4. **扫描只对结构清晰的标的打标签**：结构不清的不进结果列表，而不是塞进"其他"
   充数。扫不出来就是扫不出来。

## 阈值为什么不能写死

日线的日内振幅经常就有 2~3%，阈值给 2% 会每个 bar 都翻一次向（实测创业板指
260 根能翻出 214 个摆动点，全是噪音）。所以：
- `pct=None` 时按近 20 日平均振幅自适应：`clamp(amp×1.6, 2.0, 9.0)`
- 摆动点之间强制间隔 `min_gap` 根（默认 4），避免连续两根互为高低点。
指数波动小 → 前端显式传 1.5~2；个股/ETF → 传 3~5。

## 数据源

日线（`kline.fetch(period='day')`，东财优先、腾讯兜底，自带 120 s 缓存）。
用日线而不是分时：波浪是**波段级**结构，分时噪音会把 ZigZag 阈值打穿。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutTimeout

from ..collector import kline

# ZigZag 默认反转阈值（%）与最小间隔（根）
DEFAULT_PCT = 3.0
DEFAULT_GAP = 4
# 少于这个根数的序列不判：摆动点不够，判出来的东西是噪音
MIN_BARS = 60
# 整批扫描的墙钟上限（秒）
PROBE_TIMEOUT = 15.0
# 扫描默认看多少根日线
SCAN_N = 260

FIB_RETR = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXT = (1.0, 1.382, 1.618, 2.0, 2.618)

# 扫描标签：只保留"结构上说得清、交易上用得上"的四类
TAGS = [
    {"k": "w3", "n": "3浪主升", "d": "1、2 浪已成，正处在 3 浪推进中（通常最长最有量）"},
    {"k": "w5", "n": "5浪末端", "d": "已在第 5 浪，接近扩展目标位——空间收窄、结构风险上升"},
    {"k": "c_end", "n": "C浪到位", "d": "A-B-C 调整走完，C 浪到达常见目标区（A 浪的 0.618~1 倍）"},
    {"k": "abc", "n": "调整中", "d": "处于 A-B-C 调整过程里，方向未定"},
]
TAG_NAME = {t["k"]: t["n"] for t in TAGS}


# ── ZigZag ───────────────────────────────────────────────
def zigzag(bars: list[dict], pct: float = DEFAULT_PCT,
           min_gap: int = DEFAULT_GAP) -> list[dict]:
    """摆动高低点。返回 [{i, t, p, k}]，k ∈ {'H','L'}，严格交替。

    判定是**事后确认**的：只有当价格从极值反向走够 `pct` 才把那个极值记为摆动点，
    所以最后一个摆动点一定落后于当前价格——这是 ZigZag 的固有属性，不是 bug。

    `min_gap` 是踩过坑才加的：日线单根振幅常常就有 2~3%，没有间隔约束时相邻两根
    会互为高低点（创业板指实测 260 根翻出 214 个摆动点），全是噪音。

    **但间隔必须在事后折叠，不能写在循环里当闸门**——第一版就是写在循环里：
    确认摆动点后把游标极值设成了当根 bar 自身，于是候选极值的索引永远等于上一个
    摆动点，`i - last < gap` 恒成立，后面整整 5 个月一根摆动点都出不来（上证指数
    摆动点停在 2026-05-21）。闸门一旦关上就再也没人打开，这是这类循环的通病。
    """
    if len(bars) < 3 or pct <= 0:
        return []
    raw: list[dict] = []
    trend = 1                                   # 1 = 找高点，-1 = 找低点
    hi = bars[0]["h"]
    hi_i = 0
    lo = bars[0]["l"]
    lo_i = 0
    for i in range(1, len(bars)):
        h, l = bars[i]["h"], bars[i]["l"]
        if trend == 1:
            if hi is None or h > hi:            # 极值只从「上一个摆动点之后」的 bar 里找
                hi, hi_i = h, i
            if hi is not None and hi > 0 and l <= hi * (1 - pct / 100):
                raw.append({"i": hi_i, "t": bars[hi_i]["t"], "p": hi, "k": "H"})
                trend, lo, lo_i = -1, None, i   # None = 还没开始找下一个极值
        else:
            if lo is None or l < lo:
                lo, lo_i = l, i
            if lo is not None and lo > 0 and h >= lo * (1 + pct / 100):
                raw.append({"i": lo_i, "t": bars[lo_i]["t"], "p": lo, "k": "L"})
                trend, hi, hi_i = 1, None, i
    return _enforce_gap(raw, min_gap)


def _collapse(cluster: list[dict]) -> list[dict]:
    """一堆挨得太近的摆动点 → 各方向只留最极端的那一个（先 H 后 L，按时间排序）。"""
    if len(cluster) <= 1:
        return list(cluster)
    hs = [x for x in cluster if x["k"] == "H"]
    ls = [x for x in cluster if x["k"] == "L"]
    out: list[dict] = []
    if hs:
        out.append(max(hs, key=lambda x: x["p"]))
    if ls:
        out.append(min(ls, key=lambda x: x["p"]))
    out.sort(key=lambda x: x["i"])
    return out


def _enforce_gap(piv: list[dict], gap: int) -> list[dict]:
    """间隔折叠 + 去重：紧挨着的点合成一个高点一个低点，并抹掉同向相邻。"""
    if gap <= 1 or len(piv) < 2:
        return piv
    merged: list[dict] = []
    cluster = [piv[0]]
    for p in piv[1:]:
        if p["i"] - cluster[-1]["i"] < gap:
            cluster.append(p)
        else:
            merged.extend(_collapse(cluster))
            cluster = [p]
    merged.extend(_collapse(cluster))

    out: list[dict] = []
    for p in merged:                            # 折叠后可能出现两个相邻同向点
        if out and out[-1]["k"] == p["k"]:
            prev = out[-1]
            better = p if (p["p"] > prev["p"]) == (p["k"] == "H") else prev
            out[-1] = better
        else:
            out.append(p)
    return out


def _amp(bars: list[dict], n: int = 20) -> float:
    """近 n 根平均振幅（%），用于自适应阈值。"""
    seg = bars[-n:] if len(bars) > n else bars
    if not seg:
        return 0.0
    s = 0.0
    for b in seg:
        lo = b["l"]
        s += (b["h"] - lo) / lo * 100 if lo > 0 else 0.0
    return s / len(seg)


def auto_pct(bars: list[dict]) -> float:
    """按近 20 日平均振幅定阈值：amp×1.6，夹在 2.0~9.0。"""
    return round(max(2.0, min(9.0, _amp(bars) * 1.6)), 2)


# ── 浪型拟合 ─────────────────────────────────────────────
def _impulse(p: list[dict], up: bool) -> dict | None:
    """6 个交替摆动点 → 推动浪。up=True 时形如 L0 H1 L2 H3 L4 H5。

    返回 {score, flags, legs}；硬规则不满足返回 None。
    """
    if len(p) < 6:
        return None
    seg = p[-6:]
    ks = [x["k"] for x in seg]
    want = ["L", "H", "L", "H", "L", "H"] if up else ["H", "L", "H", "L", "H", "L"]
    if ks != want:
        return None
    v = [x["p"] for x in seg]
    if not up:                                  # 下跌浪镜像成上涨来算，省一套公式
        v = [-x for x in v]
    L0, H1, L2, H3, L4, H5 = v
    w1, w3, w5 = H1 - L0, H3 - L2, H5 - L4
    if min(w1, w3, w5) <= 0:
        return None
    if L2 <= L0:                                # 硬规则 1：2 浪不回破 1 浪起点
        return None
    if w3 < min(w1, w5):                        # 硬规则 2：3 浪不是最短
        return None
    if H5 <= H3:                                # 硬规则 3：5 浪创新高
        return None
    retr2 = (H1 - L2) / w1
    if not (0.15 <= retr2 <= 0.99):
        return None
    score = 50.0
    flags: list[str] = []
    if L4 < H1:                                 # 软规则：4 浪不进 1 浪区间
        score -= 12
        flags.append("diag")
    else:
        score += 10
    score += 10 if 0.382 <= retr2 <= 0.786 else 4
    r31 = w3 / w1
    score += 10 if 1.3 <= r31 <= 2.0 else (5 if 1.0 <= r31 <= 2.7 else 2)
    r51 = w5 / w1
    score += 8 if 0.5 <= r51 <= 1.0 else (4 if 0.3 <= r51 <= 1.62 else 1)
    return {"score": int(max(0, min(100, score))), "flags": flags,
            "legs": {"w1": w1, "w3": w3, "w5": w5, "retr2": retr2,
                     "r31": r31, "r51": r51}}


def _abc(p: list[dict], down: bool) -> dict | None:
    """4 个交替摆动点 → A-B-C 调整。down=True 时形如 H0 L1 H2 L3。"""
    if len(p) < 4:
        return None
    seg = p[-4:]
    ks = [x["k"] for x in seg]
    want = ["H", "L", "H", "L"] if down else ["L", "H", "L", "H"]
    if ks != want:
        return None
    v = [x["p"] for x in seg]
    if not down:
        v = [-x for x in v]
    H0, L1, H2, L3 = v
    a, b, c = H0 - L1, H2 - L1, H2 - L3
    if a <= 0 or b <= 0 or c <= 0:
        return None
    retr_b = b / a
    if not (0.2 <= retr_b <= 0.99):
        return None
    score = 45.0
    score += 12 if 0.382 <= retr_b <= 0.886 else 4
    r_c = c / a
    score += 12 if 0.5 <= r_c <= 1.618 else (6 if 0.382 <= r_c <= 2.0 else 2)
    if L3 < L1:                                 # C 破 A 低 = 调整结构完整
        score += 10
    return {"score": int(max(0, min(100, score))), "flags": [],
            "legs": {"a": a, "b": b, "c": c, "retr_b": retr_b, "rc": r_c}}


def _fib(lo: float, hi: float, up: bool) -> list[dict]:
    """回撤/反弹位。`up=True`：从 hi 往下的回撤位；`up=False`：从 lo 往上的反弹位。

    方向必须分开算——下跌浪的"回撤位"是反弹目标（`lo + r×range`），
    上涨浪的才是支撑（`hi - r×range`），混着用会给出一堆不可能到的价格。
    """
    rng = hi - lo
    return [{"r": r, "p": round(hi - rng * r if up else lo + rng * r, 4)}
            for r in FIB_RETR]


def _ext(base: float, unit: float, up: bool) -> list[dict]:
    """扩展目标位。up=True 向上加，否则向下减。"""
    return [{"r": r, "p": round(base + unit * r if up else base - unit * r, 4)}
            for r in FIB_EXT]


def _mirror(p: list[dict]) -> list[dict]:
    """把下跌结构镜像成上涨结构：价格取负、高低点互换。"""
    return [{"i": x["i"], "t": x["t"], "p": -x["p"],
             "k": "L" if x["k"] == "H" else "H"} for x in p]


def _best_structure(piv: list[dict], window: int = 10) -> tuple[dict, str, list[dict]] | None:
    """在最近 `window` 个摆动点里滑动找**评分最高且合规**的结构。

    只看最后 6 个点会漏掉大量结构：最后几个点常常是还没走完的尾巴。滑动窗口让
    "刚走完的那一段推动浪"也能被认出来；评分仍然只由规则决定，不是挑好看的。
    """
    best = None
    n = len(piv)
    for s in range(max(0, n - window), n):
        for pat, fn, up, need in (
                ("impulse_up", _impulse, True, 6),
                ("impulse_down", _impulse, False, 6),
                ("abc_down", _abc, True, 4),
                ("abc_up", _abc, False, 4)):
            sub = piv[s:s + need]
            if len(sub) < need:
                continue
            cand = fn(sub, up)
            if cand and (best is None or cand["score"] > best[0]["score"]):
                best = (cand, pat, sub)
    return best


def _stage_abc(seg: list[dict], down: bool, legs: dict) -> dict:
    """A-B-C 结构的 stage：C 浪是否走完、到位没有。"""
    last = seg[-1]
    rc = legs.get("rc", 0.0)
    done = (last["k"] == "L") if down else (last["k"] == "H")
    lo = min(x["p"] for x in seg)
    hi = max(x["p"] for x in seg)
    # 失效位一律取**结构起点**：调整浪的失效是"价格回到了调整开始的地方"，
    # 而不是"跌破 C 浪低点"——后者只是调整延伸，结构并没坏。
    invalid = round(seg[0]["p"], 4)
    if done:
        return {"k": "c_end" if rc >= 0.5 else "abc",
                "n": "C浪到位" if rc >= 0.5 else "调整中",
                "desc": f"C 浪 = A 浪的 {rc:.2f} 倍，{'进入常见目标区' if rc >= 0.5 else '尚未走满'}",
                "targets": _fib(lo, hi, not down),
                "invalid": invalid}
    return {"k": "abc", "n": "调整中",
            "desc": "A-B-C 调整进行中，方向未定（C 浪未走完）",
            "targets": _fib(lo, hi, down),
            "invalid": invalid}


def _stage_of(p: list[dict], price: float) -> dict:
    """根据已确认摆动点的**个数与方向**推断当前处在哪个浪（假定结构是向上的）。

    这是全模块主观性最强的一块，所以每条结论都带 `desc` 说清依据、带 `invalid`
    给出结构被破坏的价格——判错了能立刻认出来。
    """
    n = len(p)
    last = p[-1]
    if n < 3:
        return {"k": "na", "n": "结构不足", "desc": "摆动点少于 3 个，判不出浪型",
                "targets": [], "invalid": None}
    if last["k"] == "H":
        # 最后一个摆动点是高点：要么 5 浪走完，要么正在回调
        if n >= 6:
            return {"k": "after5", "n": "5浪末端",
                    "desc": "1-5 浪结构已走完，随后通常是 A-B-C 调整",
                    "targets": [], "invalid": round(p[-2]["p"], 4)}
        prev_l = p[-2]["p"]
        return {"k": "pullback", "n": "回调中",
                "desc": f"从高点 {last['p']:.2f} 回落，{prev_l:.2f} 是关键支撑",
                "targets": _fib(prev_l, last["p"], True),
                "invalid": round(prev_l, 4)}
    # 最后一个摆动点是低点：正在往上走，看越过了哪个前高
    prev_h = p[-2]["p"]
    unit = last["p"] - p[-3]["p"] if n >= 3 else 0.0
    if price > prev_h:
        lvl = "3浪" if n <= 4 else "5浪"
        return {"k": "w3_up" if n <= 4 else "w5_up", "n": f"{lvl}推进中",
                "desc": f"越过前一高点 {prev_h:.2f}，{lvl}推进中",
                "targets": _ext(last["p"], abs(unit), True) if unit else [],
                "invalid": round(last["p"], 4)}
    return {"k": "pullback", "n": "回调中",
            "desc": f"在 {last['p']:.2f}~{prev_h:.2f} 区间内整理，方向待定",
            "targets": _fib(last["p"], prev_h, False),
            "invalid": round(last["p"], 4)}


# ── 主分析 ───────────────────────────────────────────────
def analyze(bars: list[dict], pct: float | None = None,
            code: str = "", name: str = "",
            min_gap: int = DEFAULT_GAP) -> dict:
    """单标的波浪分析。bars 是日线 [{t,o,c,h,l,v,amt}]。pct=None → 自适应。"""
    res: dict = {"ok": False, "code": code, "name": name,
                 "n_bars": len(bars), "pattern": None, "waves": [], "pivots": [],
                 "stage": {"k": "na", "n": "无数据", "desc": "", "targets": [],
                           "invalid": None},
                 "fib": {"retr": [], "ext": []}, "confidence": 0,
                 "flags": [], "legs": {}, "na": [], "note": ""}
    amp20 = _amp(bars) if bars else 0.0
    if pct is None:
        pct = auto_pct(bars) if bars else DEFAULT_PCT
    # pct 提前回填：数据不足时也要让前端知道用的是哪个阈值，否则面板上显示空
    res["pct"], res["amp20"], res["min_gap"] = pct, round(amp20, 2), min_gap
    if not bars or len(bars) < MIN_BARS:
        res["na"].append(f"日线不足 {MIN_BARS} 根（当前 {len(bars)}），波段结构判不了")
        return res

    piv = zigzag(bars, pct, min_gap)
    price = bars[-1]["c"]
    res["pivots"] = piv
    res["price"] = round(price, 4)
    if len(piv) < 3:
        res["na"].append(
            f"阈值 {pct}% 下只找到 {len(piv)} 个摆动点（近 20 日平均振幅 "
            f"{amp20:.2f}%），把灵敏度调小再试")
        return res

    best = _best_structure(piv)
    if best is None:
        res["na"].append("摆动点不满足推动浪硬规则（2 浪不破起点 / 3 浪不是最短 / "
                         "5 浪创新高）或 A-B-C 结构，本次不给浪型——不硬套")
        res["stage"] = {"k": "na", "n": "无合规浪型",
                        "desc": "这段行情数不出规则内的浪型，换个阈值或换个周期再看",
                        "targets": [], "invalid": None}
        return res

    cand, pat, seg = best
    res["ok"] = True
    res["pattern"] = pat
    res["confidence"] = cand["score"]
    res["flags"] = cand["flags"]
    res["legs"] = {k: round(v, 4) for k, v in cand["legs"].items()}
    n_mark = len(seg)
    if pat.startswith("impulse"):
        lab = ["起点", "1", "2", "3", "4", "5"]
    elif pat == "abc_down":
        lab = ["起点", "A", "B", "C"]
    else:
        lab = ["起点", "A", "B", "C"]
    for x, lb in zip(seg, lab):
        res["waves"].append({"i": x["i"], "t": x["t"], "p": round(x["p"], 4),
                             "k": x["k"], "label": lb})

    # 下跌结构统一镜像成上涨来算 stage 与斐波那契，最后把价格翻回来
    down = pat in ("impulse_down", "abc_up")
    if pat.startswith("impulse"):
        pv = _mirror(seg) if down else seg
        st = _stage_of(pv, -price if down else price)
        if down:                                    # 镜像价格翻回真实价
            st = {"k": st["k"], "n": st["n"], "desc": st["desc"],
                  "targets": [{"r": t["r"], "p": round(-t["p"], 4)}
                              for t in st["targets"]],
                  "invalid": (round(-st["invalid"], 4)
                              if st.get("invalid") is not None else None)}
    else:
        st = _stage_abc(seg, pat == "abc_down", cand["legs"])
    lo_p, hi_p = min(x["p"] for x in seg), max(x["p"] for x in seg)
    up_struct = seg[-1]["k"] == "H"             # 结构收在高点 → 之后看回撤支撑
    res["fib"]["retr"] = _fib(lo_p, hi_p, up_struct)
    if pat.startswith("impulse"):
        w1 = cand["legs"].get("w1") or 0.0
        base = seg[-2]["p"]
        res["fib"]["ext"] = _ext(base, w1, not down)
    else:
        res["fib"]["ext"] = _ext(seg[-1]["p"], cand["legs"].get("a") or 0.0, not down)
    res["stage"] = st
    res["note"] = ("波浪数法是解释性框架不是预测器；同一段行情可能有多种合规数法。"
                   "摆动点与斐波那契位是客观计算，'当前第几浪'是概率判断，配合失效位用。")
    return res


def _tag_of(an: dict) -> str | None:
    k = (an.get("stage") or {}).get("k")
    if k == "w3_up":
        return "w3"
    if k in ("w5_up", "after5"):
        return "w5"
    if k in ("c_end", "abc"):
        return k
    return None


def _market(code: str) -> str:
    if code.startswith(("60", "68", "51", "58", "56", "11")):
        return "sh"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


# ── 全市场扫描 ───────────────────────────────────────────
def scan(quotes: list, *, flt: dict | None = None, probe: int = 60,
         size: int = 50, pct: float | None = None, min_conf: int = 55,
         tags: str = "", sort: str = "conf") -> dict:
    """两级漏斗：快照粗筛 → 拉日线算浪型。

    A 层用快照（0 请求）把池子压到成交额靠前的 `probe` 只；B 层并发拉日线。
    `probe_ok` 上报真正拿到日线的只数——源挂掉时它是 0，此时**一条结论都没有**，
    前端必须如实显示，不能默默给个空列表。
    """
    flt = flt or {}
    want = {t.strip() for t in (tags or "").split(",") if t.strip()}
    t0 = time.monotonic()

    pool: list = []
    for q in quotes:
        if getattr(q, "is_halt", False):
            continue
        if flt.get("excl_st", 1) and getattr(q, "is_st", False):
            continue
        if getattr(q, "price", 0) <= 0:
            continue
        a_min = flt.get("amt_min")
        if a_min not in (None, "") and getattr(q, "amt_wan", 0.0) < float(a_min) * 1e4:
            continue
        mk = flt.get("boards") or ""
        if mk:
            want_mk = {x.strip() for x in mk.split(",") if x.strip()}
            if _market(getattr(q, "code", "")) not in want_mk:
                continue
        pool.append(q)

    # 按成交额取前 probe 只：日线是稀缺资源（每只一次请求），先给最活跃的
    pool.sort(key=lambda q: getattr(q, "amt_wan", 0.0), reverse=True)
    probed = pool[:max(0, min(int(probe), 120))]

    bars_map: dict[str, list[dict]] = {}
    timed_out = 0
    if probed:
        ex = ThreadPoolExecutor(max_workers=min(12, max(6, len(probed))))
        futs = {ex.submit(kline.fetch, q.code, "day", SCAN_N): q.code for q in probed}
        try:
            for f in as_completed(futs, timeout=PROBE_TIMEOUT):
                try:
                    d = f.result()
                except Exception:                      # noqa: BLE001
                    d = None
                if d and d.get("bars"):
                    bars_map[futs[f]] = d["bars"]
        except FutTimeout:
            timed_out = len(futs) - len(bars_map)
        finally:
            ex.shutdown(wait=False)

    rows: list[dict] = []
    for q in probed:
        bars = bars_map.get(q.code)
        if not bars:
            continue
        an = analyze(bars, pct, q.code, getattr(q, "name", ""))
        if not an.get("ok") or an["confidence"] < min_conf:
            continue
        tag = _tag_of(an)
        if not tag or (want and tag not in want):
            continue
        st = an["stage"]
        rows.append({
            "c": q.code, "n": getattr(q, "name", ""),
            "p": getattr(q, "price", 0.0), "pc": getattr(q, "pct", 0.0),
            "amt": round(getattr(q, "amt_wan", 0.0) / 1e4, 2),
            "tag": tag, "tag_n": TAG_NAME[tag],
            "stage": st.get("n") or "", "why": st.get("desc") or "",
            "conf": an["confidence"], "pattern": an["pattern"],
            "tgt": (st.get("targets") or [{}])[0].get("p"),
            "inv": st.get("invalid"),
            "waves": an["waves"], "fib": an["fib"],
        })

    if sort == "pct":
        rows.sort(key=lambda r: r["pc"], reverse=True)
    elif sort == "amt":
        rows.sort(key=lambda r: r["amt"], reverse=True)
    else:
        rows.sort(key=lambda r: (r["conf"], r["pc"]), reverse=True)

    return {
        "rows": rows[:size], "total": len(rows),
        "pool": len(pool), "probed": len(probed),
        "probe_ok": len(bars_map),
        "timed_out": timed_out,
        "pct": pct, "auto_pct": pct is None,
        "min_conf": min_conf, "tags": tags,
        "ms": round((time.monotonic() - t0) * 1000),
        "modes": TAGS,
        "na": ["分时级浪型（日线判波段，分时噪音会打穿阈值）",
               "基本面/题材对浪型的影响（不进计算）"],
    }
