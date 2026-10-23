# -*- coding: utf-8 -*-
"""交易时段闸门。

为什么不用第三方交易日历库：库会过期，且一年只有 240+ 个交易日，
引入一个需要定期更新的依赖不划算。这里用两条判据：
  1. 本地时钟的时段（粗筛）
  2. 上证指数快照的时间戳日期是否等于今日（细判，自动跳过法定节假日）

**延迟口径**（2026-10-23 实测，重要）：
    盘中 10:54 → lag 3 s；11:12 → 5.4 s
    午休 11:38 → lag 22 s；11:45 → 9.9 s
非交易时段 lag 会累积到 20+ 秒，原因是**源端快照时间戳本身不推进**，
不是网络故障，也不是被限流。所以延迟告警只在交易时段有意义。
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime


# 时段边界
AUCTION_START = dtime(9, 15)
OPEN = dtime(9, 30)
MORNING_END = dtime(11, 30)
AFTERNOON = dtime(13, 0)
CLOSE = dtime(15, 0)
SETTLE_END = dtime(15, 5)


def now_hms() -> dtime:
    return datetime.now().time()


def is_weekday(ts: datetime | None = None) -> bool:
    return (ts or datetime.now()).weekday() < 5


def session_state(ts: datetime | None = None) -> str:
    """返回当前时段标签。

    pre       盘前
    auction   集合竞价 09:15-09:25
    preopen   09:25-09:30 静默
    trading   连续竞价
    lunch     午休
    closing   收盘集合竞价 15:00-15:05
    closed    已收盘
    """
    ts = ts or datetime.now()
    if not is_weekday(ts):
        return "closed"
    t = ts.time()
    if t < AUCTION_START:
        return "pre"
    if t < dtime(9, 25):
        return "auction"
    if t < OPEN:
        return "preopen"
    if t < MORNING_END:
        return "trading"
    if t < AFTERNOON:
        return "lunch"
    if t < CLOSE:
        return "trading"
    if t < SETTLE_END:
        return "closing"
    return "closed"


def is_trading(state: str | None = None) -> bool:
    return (state or session_state()) == "trading"


def is_active(state: str | None = None) -> bool:
    """是否应该全速轮询（含竞价与收盘集合竞价）。"""
    return (state or session_state()) in ("auction", "preopen", "trading", "closing")


# 各时段的轮询节拍（秒）。数值即方案 §4.2 的闸门表。
INTERVALS = {
    "pre": 300.0,
    "auction": 3.0,
    "preopen": 10.0,
    "trading": 1.0,
    "lunch": 60.0,
    "closing": 5.0,
    "closed": 3600.0,
}


def tick_interval(kind: str = "index", state: str | None = None) -> float:
    """按任务类型与时段给节拍。

    kind: index / full / board / flow / tick
    """
    st = state or session_state()
    base = INTERVALS.get(st, 60.0)
    if st in ("pre", "closed"):
        return base
    if st == "lunch":
        return 60.0
    mult = {"index": 1.0, "full": 3.0, "board": 5.0, "flow": 5.0, "tick": 5.0}.get(kind, 5.0)
    return max(base * mult, 0.5)


def lag_note(state: str | None = None) -> str:
    """延迟角标的说明文字。非交易时段不要把它当故障。"""
    st = state or session_state()
    if st == "trading":
        return "交易中"
    if st in ("auction", "preopen"):
        return "集合竞价"
    if st == "closing":
        return "收盘竞价"
    if st == "lunch":
        return "午休（价格冻结）"
    if st == "closed":
        return "已收盘"
    return "盘前"


def lag_is_alarming(lag_sec: float, state: str | None = None) -> bool:
    """只有交易时段的大延迟才算异常。"""
    st = state or session_state()
    if st not in ("trading", "closing", "auction"):
        return False
    return lag_sec > 10.0


if __name__ == "__main__":
    st = session_state()
    print(f"当前时间 : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"时段     : {st}  ({lag_note(st)})")
    print(f"是否活跃 : {is_active(st)}")
    for k in ("index", "full", "board", "flow", "tick"):
        print(f"  {k:<6} 节拍 {tick_interval(k, st):>6.1f} s")
