# -*- coding: utf-8 -*-
"""腾讯实时快照采集与解析。

接口：GET https://qt.gtimg.cn/q=sh600519,sz000001,...
- 返回 GBK，字段以 ~ 分隔，**无字段名，全靠位置索引**
- 不同标的字段数不同（沪市个股 88、北证50 87）→ 解析必须做长度防御
- 单批 300 只实测 146 KB / 热 53 ms；全市场 5343 只 18 批 6 并发 218 ms

字段位（2026-10-23 逐位核对，详见 docs/数据源手册.md）：
  [1]名称 [2]代码 [3]现价 [4]昨收 [5]今开 [6]量(手) [7]外盘 [8]内盘
  [9-18]买一~买五(价,量) [19-28]卖一~卖五(价,量) [30]时间戳
  [31]涨跌额 [32]涨跌幅% [33]最高 [34]最低 [37]成交额(万) [38]换手%
  [39]PE [43]振幅% [44]流通市值(亿) [45]总市值(亿) [46]PB
  [47]涨停价 [48]跌停价 [49]量比
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from ..config import (BAD_RATE_WARN, PCT_MAX, PRICE_TOL, TX_BATCH, TX_QPS,
                      TX_TIMEOUT, TX_URL, TX_WORKERS)
from ..models import ParseStats, Quote
from .channel import BASE_HEADERS, HttpChannel

_tx = HttpChannel(hosts=["qt.gtimg.cn"], qps=TX_QPS, timeout=TX_TIMEOUT, tries=2)


def sym_of(code: str) -> str:
    """代码 → 腾讯符号。

    北交所是最大的坑：920xxx 若判成 sh 会返回空，而且**不报错**（静默失败）。
    """
    c = str(code).zfill(6)
    if c[:2] == "92" or c[0] in "48" or c[:2] in ("43", "83", "87", "88"):
        return "bj" + c
    if c[0] in "69":
        return "sh" + c
    return "sz" + c


def _f(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v: str) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _lag_sec(ts: str) -> float:
    """源端时间戳与本机的差（秒）。这是真实行情延迟，不是我们的网络耗时。"""
    if not ts or len(ts) < 14:
        return 0.0
    try:
        t = time.mktime(time.strptime(ts[:14], "%Y%m%d%H%M%S"))
        return round(time.time() - t, 1)
    except (ValueError, OverflowError):
        return 0.0


def parse_line(key: str, body: str, stats: ParseStats) -> Quote | None:
    """解析单行。任何不自洽都计入 stats 并返回 None——坏数据绝不进 UI。"""
    f = body.split("~")
    stats.total += 1

    # 最少需要到 [49]（量比）；指数与个股数量不同，这里只做下限防御
    if len(f) < 50:
        stats.bad_field += 1
        if len(stats.messages) < 5:
            stats.messages.append(f"{key}: 字段数不足 {len(f)}")
        return None

    try:
        price, prev, chg, pct = _f(f[3]), _f(f[4]), _f(f[31]), _f(f[32])
        vol_raw = _i(f[6])
        amt_wan = _f(f[37])

        # 停牌：无价无成交。放行，但不参与价格类统计
        if price <= 0 and vol_raw <= 0:
            stats.total -= 1
            stats.halted += 1
            return None

        # 断言一：价格三元组自洽（字段错位的最强信号）
        if prev > 0 and abs(price - prev - chg) > PRICE_TOL:
            stats.bad_price += 1
            if len(stats.messages) < 5:
                stats.messages.append(f"{key}: price={price} prev={prev} chg={chg} 不自洽")
            return None

        # 断言二：涨跌幅不可能超过 31%（北交所 30% + 余量）
        if abs(pct) > PCT_MAX:
            stats.bad_pct += 1
            if len(stats.messages) < 5:
                stats.messages.append(f"{key}: pct={pct} 越界")
            return None

        # ── 成交量单位归一 ──────────────────────────────────
        # 腾讯对**科创板个股（688xxx）返回「股」，其余返回「手」**（2026-10-23 实测：
        # sh688001 [6]=8666261 股 × 40.22 ≈ 3.49 亿 ≈ 成交额 3.53 亿；
        # sh600519 [6]=35148 手 × 100 × 1339 ≈ 470 亿 ≈ 成交额 467 亿）。
        # 用 [35]="价/量/成交额(元)" 里的成交额反推每单位股数，不靠代码前缀硬编码。
        amt_yuan = 0.0
        seg = f[35].split("/") if len(f) > 35 else []
        if len(seg) >= 3:
            amt_yuan = _f(seg[2])
        if amt_yuan <= 0:
            amt_yuan = amt_wan * 1e4

        share_per_unit = 100                      # 默认：1 手 = 100 股
        if amt_yuan > 0 and vol_raw > 0 and price > 0:
            ratio = (amt_yuan / vol_raw) / price
            # 量单位是「股」→ 成交额/量 ≈ 现价（ratio≈1）
            # 量单位是「手」→ 成交额/量 ≈ 现价×100（ratio≈100）
            if 0.5 < ratio < 2.0:
                share_per_unit = 1

        # 断言三：均价与现价不应差 2 倍以上（防单位错位、防万元/元混用）
        if amt_yuan > 0 and vol_raw > 0:
            implied = amt_yuan / vol_raw
            expect = price * share_per_unit
            if expect > 0 and not (0.5 < implied / expect < 2.0):
                stats.bad_amount += 1
                if len(stats.messages) < 5:
                    stats.messages.append(
                        f"{key}: 均价倒推 {implied:.3f} vs 现价×单位 {expect:.3f} 不符")
                return None

        # 对外统一用「手」
        vol = int(vol_raw * share_per_unit / 100)

        bids = tuple((_f(f[9 + 2 * i]), _i(f[10 + 2 * i])) for i in range(5))
        asks = tuple((_f(f[19 + 2 * i]), _i(f[20 + 2 * i])) for i in range(5))
        pe = _f(f[39])
        pb = _f(f[46])

        stats.ok += 1
        return Quote(
            code=f[2] or key[-6:],
            name=f[1],
            sym=key,
            price=price, prev=prev, open=_f(f[5]), high=_f(f[33]), low=_f(f[34]),
            chg=chg, pct=pct,
            vol_hand=vol, amt_wan=amt_wan,
            turnover=_f(f[38]), vol_ratio=_f(f[49]), amplitude=_f(f[43]),
            pe=pe if pe > 0 else None, pb=pb if pb > 0 else None,
            total_cap_yi=_f(f[45]), float_cap_yi=_f(f[44]),
            limit_up=_f(f[47]), limit_dn=_f(f[48]),
            # 外盘/内盘与 [6] 同单位，必须同步归一，否则内外盘比会错 100 倍
            out_vol=int(_i(f[7]) * share_per_unit / 100),
            in_vol=int(_i(f[8]) * share_per_unit / 100),
            bid1=bids[0][0], bid1_vol=bids[0][1],
            ask1=asks[0][0], ask1_vol=asks[0][1],
            bids=bids, asks=asks,
            src_ts=f[30], lag_sec=_lag_sec(f[30]),
        )
    except (IndexError, ValueError) as e:
        stats.bad_field += 1
        if len(stats.messages) < 5:
            stats.messages.append(f"{key}: 解析异常 {e!r}")
        return None


def _fetch_batch(symbols: list[str], stats: ParseStats) -> dict[str, Quote]:
    raw = _tx.get_text(TX_URL + ",".join(symbols), encoding="gbk")
    out: dict[str, Quote] = {}
    for line in raw.split(";"):
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].replace("v_", "").strip()
        body = line.split('"')[1]
        q = parse_line(key, body, stats)
        if q is not None:
            out[q.sym] = q
    return out


def fetch_many(symbols: list[str], workers: int = TX_WORKERS,
               batch: int = TX_BATCH) -> tuple[dict[str, Quote], ParseStats, float]:
    """批量拉取。返回 (以 sym 为键的快照, 自检统计, 墙钟耗时毫秒)。"""
    stats = ParseStats()
    batches = [symbols[i:i + batch] for i in range(0, len(symbols), batch)]
    t0 = time.perf_counter()
    merged: dict[str, Quote] = {}
    if len(batches) <= 1:
        for b in batches:
            merged.update(_fetch_batch(b, stats))
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(lambda b: _fetch_batch(b, stats), batches):
                merged.update(part)
    elapsed = (time.perf_counter() - t0) * 1000
    return merged, stats, round(elapsed, 1)


def fetch_codes(codes: list[str], **kw) -> tuple[dict[str, Quote], ParseStats, float]:
    return fetch_many([sym_of(c) for c in codes], **kw)


def channel_stats() -> dict:
    return _tx.stats()


def should_warn(stats: ParseStats) -> bool:
    return stats.bad_rate > BAD_RATE_WARN
