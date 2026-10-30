# -*- coding: utf-8 -*-
"""L1 热存储：进程内存里的最新快照。

为什么不是 Redis（方案 §3.3）：单进程单 worker，读一次 dict 约 100 ns，
Redis 要走一次 TCP 往返（约 200 µs），慢 3 个数量级；而本场景读负载不到 20 QPS。
**唯一的抽象边界就是本文件**：将来要换 Redis，只改 get/set/snapshot 几个函数。

线程模型：
- 采集在 asyncio.to_thread 里跑（urllib 是同步的），写入走 threading.RLock
- 读取方（HTTP 处理、WS 广播）拿的是浅拷贝，不持有锁做序列化
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

from .config import (ALERT_DEDUP_SEC, ALERT_KEEP, PCT_WINDOW_KEEP)
from .models import Alert, Board, Quote, TickTrade


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()

        # 行情
        self.quotes: dict[str, Quote] = {}        # code -> 最新
        self.prev: dict[str, Quote] = {}          # code -> 上一轮（异动 diff 用）
        self.indices: dict[str, Quote] = {}       # sym -> 指数

        # 板块
        self.boards: dict[str, Board] = {}
        self.board_kind: dict[str, str] = {}

        # 资金流
        self.market_flow: list[dict] = []         # [{t, main, small, medium, large, super}]
        self.stock_flow: list[dict] = []          # Top N 个股主力净流入
        self.board_flow: list[dict] = []          # Top N 板块主力净流入
        # 详情抽屉按代码取，别再线性扫 stock_flow（120 行倒不慢，但每次点开都扫一遍没必要）
        self.stock_flow_map: dict[str, dict] = {}
        self.flow_meta: dict = {}                 # {"src": "em|sina", "pool": N, "label": "..."}

        # 逐笔与异动
        self.ticks: dict[str, deque[TickTrade]] = {}
        self.alerts: deque[Alert] = deque(maxlen=ALERT_KEEP)
        self._alert_seen: dict[tuple[str, str], float] = {}

        # 涨速采样：code -> deque[(mono, pct)]
        self._pct_hist: dict[str, deque[tuple[float, float]]] = {}

        # 推送签名：只有真正变化的标的才进帧
        self._sig: dict[str, tuple] = {}

        # 各数据块的健康信息
        self.meta: dict[str, dict] = {}
        self.parse_stats: dict = {}
        self.breadth: dict = {}
        self.thrift = False

    # ── 行情 ────────────────────────────────────────────
    def apply_quotes(self, batch: dict[str, Quote]) -> list[Quote]:
        """覆盖写，返回本轮内容有变化的标的（顺序无关）。"""
        changed: list[Quote] = []
        with self._lock:
            for code, q in batch.items():
                old = self.quotes.get(code)
                sig = (q.price, q.pct, q.vol_hand, q.amt_wan,
                       q.bid1, q.bid1_vol, q.ask1, q.ask1_vol)
                if old is not None:
                    self.prev[code] = old
                self.quotes[code] = q
                if self._sig.get(code) != sig:
                    self._sig[code] = sig
                    changed.append(q)
        return changed

    def apply_indices(self, batch: dict[str, Quote]) -> list[Quote]:
        changed: list[Quote] = []
        with self._lock:
            for sym, q in batch.items():
                old = self.indices.get(sym)
                if old is None or (old.price, old.pct) != (q.price, q.pct):
                    changed.append(q)
                self.indices[sym] = q
        return changed

    def get(self, code: str) -> Quote | None:
        return self.quotes.get(code)

    def get_prev(self, code: str) -> Quote | None:
        return self.prev.get(code)

    def snapshot_quotes(self) -> list[Quote]:
        with self._lock:
            return list(self.quotes.values())

    def snapshot_indices(self) -> list[Quote]:
        with self._lock:
            return list(self.indices.values())

    def count(self) -> int:
        return len(self.quotes)

    # ── 板块 ────────────────────────────────────────────
    def apply_boards(self, kind: str, rows: list[Board]) -> None:
        """整类替换，不是增量合并。

        合并会留两个坑：① 换源（东财 BKxxxx ↔ 新浪 new_*/gn_*/diyu_*）后两套代码并存，
        同一个"互联金融"出现两条；② 东财某轮少返回几只，旧记录永远清不掉。
        fetch_boards 要么返回完整一维、要么抛异常，所以整类替换是安全的。
        """
        keep = {b.code for b in rows}
        with self._lock:
            stale = [c for c, k in self.board_kind.items()
                     if k == kind and c not in keep]
            for c in stale:
                self.boards.pop(c, None)
                self.board_kind.pop(c, None)
            for b in rows:
                self.boards[b.code] = b
                self.board_kind[b.code] = kind

    def snapshot_boards(self, kind: str | None = None) -> list[Board]:
        with self._lock:
            if kind is None:
                return list(self.boards.values())
            return [b for b in self.boards.values() if b.kind == kind]

    def board(self, code: str) -> Board | None:
        return self.boards.get(code)

    # ── 资金流 ──────────────────────────────────────────
    def apply_flow(self, *, market: list[dict] | None = None,
                   stocks: list[dict] | None = None,
                   boards: list[dict] | None = None,
                   src: str | None = None, pool: int | None = None,
                   label: str | None = None) -> None:
        with self._lock:
            if market is not None:
                self.market_flow = market
            if stocks is not None:
                self.stock_flow = stocks
                self.stock_flow_map = {r["c"]: r for r in stocks if r.get("c")}
            if boards is not None:
                self.board_flow = boards
            if src is not None:
                self.flow_meta = {"src": src, "pool": pool or 0, "label": label or ""}

    def flow_of(self, code: str) -> dict | None:
        """个股资金流。只覆盖样本池内 + 最近点开过的标的。"""
        return self.stock_flow_map.get(code)

    # ── 逐笔 ────────────────────────────────────────────
    def push_ticks(self, code: str, trades: list[TickTrade], keep: int = 200) -> None:
        with self._lock:
            buf = self.ticks.get(code)
            if buf is None:
                buf = deque(maxlen=keep)
                self.ticks[code] = buf
            buf.extend(trades)

    def get_ticks(self, code: str, n: int = 100) -> list[TickTrade]:
        with self._lock:
            buf = self.ticks.get(code)
            if not buf:
                return []
            return list(buf)[-n:]

    # ── 异动 ────────────────────────────────────────────
    def add_alerts(self, items: list[Alert]) -> list[Alert]:
        """按 (code, kind) 在窗口内去重，避免同一事件每轮刷屏。"""
        now = time.monotonic()
        fresh: list[Alert] = []
        with self._lock:
            for a in items:
                key = (a.code, a.kind)
                last = self._alert_seen.get(key, 0.0)
                if now - last < ALERT_DEDUP_SEC:
                    continue
                self._alert_seen[key] = now
                self.alerts.append(a)
                fresh.append(a)
            # 键太多会慢慢涨，定期清一次
            if len(self._alert_seen) > 20000:
                self._alert_seen = {k: v for k, v in self._alert_seen.items()
                                    if now - v < ALERT_DEDUP_SEC * 4}
        return fresh

    def snapshot_alerts(self, n: int = 100, kind: str | None = None) -> list[Alert]:
        with self._lock:
            items = list(self.alerts)
        if kind:
            items = [a for a in items if a.kind == kind]
        return items[-n:][::-1]      # 最新在前

    # ── 涨速采样 ────────────────────────────────────────
    def sample_pct(self) -> None:
        """每 30 s 采样一次 pct。涨速规则靠这个序列，不给全市场存 tick 历史。"""
        now = time.monotonic()
        with self._lock:
            for code, q in self.quotes.items():
                buf = self._pct_hist.get(code)
                if buf is None:
                    buf = deque(maxlen=PCT_WINDOW_KEEP)
                    self._pct_hist[code] = buf
                buf.append((now, q.pct))

    def speed_of(self, code: str, window: float) -> float | None:
        """返回 window 秒前到现在的 pct 变化（百分点）；样本不足返回 None。"""
        with self._lock:
            buf = self._pct_hist.get(code)
            if not buf or len(buf) < 2:
                return None
            now_ts, now_pct = buf[-1]
            best = None
            for ts, pct in buf:
                if now_ts - ts >= window:
                    best = (ts, pct)
                    break
            if best is None:
                return None
            return round(now_pct - best[1], 3)

    # ── 健康信息 ────────────────────────────────────────
    def touch(self, key: str, **info) -> None:
        with self._lock:
            rec = self.meta.setdefault(key, {})
            rec.update(info)
            rec["ts"] = _iso_now()
            rec["mono"] = time.monotonic()

    def staleness(self, key: str) -> float | None:
        rec = self.meta.get(key)
        if not rec:
            return None
        return round(time.monotonic() - rec["mono"], 1)

    # ── 序列化 ──────────────────────────────────────────
    def quote_full(self, q: Quote, with_book: bool = True) -> dict:
        d = {
            "c": q.code, "n": q.name, "sym": q.sym,
            "p": q.price, "pc": q.pct, "chg": q.chg, "prev": q.prev,
            "o": q.open, "h": q.high, "l": q.low,
            "v": q.vol_hand, "amt": round(q.amt_wan / 1e4, 3),   # 亿元
            "to": q.turnover, "vr": q.vol_ratio, "amp": q.amplitude,
            "pe": q.pe, "pb": q.pb,
            "cap": q.total_cap_yi, "fcap": q.float_cap_yi,
            "lu": q.limit_up, "ld": q.limit_dn,
            "out": q.out_vol, "in": q.in_vol, "oir": q.outer_inner_ratio,
            "st": q.is_st, "halt": q.is_halt,
            "zt": q.is_limit_up, "dt": q.is_limit_down,
            "lag": q.lag_sec, "ts": q.src_ts,
        }
        if with_book:
            d["b"] = [[p, v] for p, v in q.bids]
            d["a"] = [[p, v] for p, v in q.asks]
            d["b1"] = q.bid1
            d["b1v"] = q.bid1_vol
            d["a1"] = q.ask1
            d["a1v"] = q.ask1_vol
        return d

    def short_row(self, q: Quote) -> dict:
        """排行用的极简字段（方案 §8.1）。字段名短是为了压帧体积。"""
        return {
            "c": q.code, "n": q.name, "p": q.price, "pc": q.pct,
            "vr": q.vol_ratio, "to": q.turnover,
            "amt": round(q.amt_wan / 1e4, 2),      # 亿元
            "amp": q.amplitude, "lu": q.limit_up,
            "cap": round(q.total_cap_yi, 1),
            "zt": q.is_limit_up, "dt": q.is_limit_down,
            "st": q.is_st, "halt": q.is_halt,
        }

    def diff_payload(self, q: Quote) -> dict:
        """增量帧的最小字段集。前端浅合并，不做整行替换。"""
        return {
            "c": q.code, "p": q.price, "pc": q.pct,
            "v": q.vol_hand, "amt": round(q.amt_wan / 1e4, 3),
            "b1": q.bid1, "b1v": q.bid1_vol,
            "a1": q.ask1, "a1v": q.ask1_vol,
            "vr": q.vol_ratio, "to": q.turnover,
        }


STORE = Store()
