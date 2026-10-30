# -*- coding: utf-8 -*-
"""市场宽度聚合。

口径（方案 §5.1，写清楚否则两个月后自己看不懂）：
- 分母 = 自建清单里的在册标的数；停牌单独计数，**不计入平盘**
- 涨跌停用快照的 limit_up / limit_dn 字段判，不用百分比——北交所 30%、
  ST 5%、创业/科创 20%，源端已经算好，比自己写规则可靠
- 「沪深成交额」排除北交所（bj），单列 amt_bj_yi
"""
from __future__ import annotations

from ..models import Quote

# 12 条边界 → 11 个桶，与方案 §5.1 一致
HIST_EDGES = (-1e9, -9, -7, -5, -3, -1, 0, 1, 3, 5, 7, 9, 1e9)


def _bucket(pct: float) -> int:
    for i in range(len(HIST_EDGES) - 1):
        if HIST_EDGES[i] <= pct < HIST_EDGES[i + 1]:
            return i
    return len(HIST_EDGES) - 2


def compute(quotes: list[Quote], lag_sec: float = 0.0) -> dict:
    total = len(quotes)
    up = down = flat = halt = 0
    zt = dt = 0
    amt_wan_sh_sz = 0.0
    amt_wan_bj = 0.0
    hist = [0] * (len(HIST_EDGES) - 1)
    counts = {"up5": 0, "dn5": 0}      # 涨超 5% / 跌超 5%：情绪用

    for q in quotes:
        if q.is_halt:
            halt += 1
            continue

        if q.pct > 0:
            up += 1
        elif q.pct < 0:
            down += 1
        else:
            flat += 1

        if q.pct >= 5:
            counts["up5"] += 1
        elif q.pct <= -5:
            counts["dn5"] += 1

        if q.is_limit_up:
            zt += 1
        elif q.is_limit_down:
            dt += 1

        hist[_bucket(q.pct)] += 1

        if q.exchange == "bj":
            amt_wan_bj += q.amt_wan
        else:
            amt_wan_sh_sz += q.amt_wan

    # 恒等式自校验：数据源少回一批时，这里会立刻炸出来，
    # 而不是让你盯着"涨 2000 跌 1800"自我怀疑
    if up + down + flat + halt != total:
        raise AssertionError(
            f"宽度恒等式不成立: {up}+{down}+{flat}+{halt} != {total}")

    return {
        "total": total,
        "up": up, "down": down, "flat": flat, "halt": halt,
        "zt": zt, "dt": dt,
        "up5": counts["up5"], "dn5": counts["dn5"],
        "ratio": round(up / max(down, 1), 3),          # 涨跌比
        "amt_yi": round(amt_wan_sh_sz / 1e4, 1),       # 亿元（沪深）
        "amt_bj_yi": round(amt_wan_bj / 1e4, 1),
        "hist": hist,
        "lag_sec": lag_sec,
    }


def empty() -> dict:
    return {"total": 0, "up": 0, "down": 0, "flat": 0, "halt": 0,
            "zt": 0, "dt": 0, "up5": 0, "dn5": 0, "ratio": 0.0,
            "amt_yi": 0.0, "amt_bj_yi": 0.0,
            "hist": [0] * (len(HIST_EDGES) - 1), "lag_sec": 0.0}
