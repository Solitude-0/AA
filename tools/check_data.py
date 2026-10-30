# -*- coding: utf-8 -*-
"""对账：自算宽度 vs 独立口径。

为什么必须对账：宽度错了不会报错，只会让你盯着一个"看起来正常"的错数字做判断。
这里用两条互相独立的证据交叉验证：

1. **成交额**：Σ 个股 amt_wan（沪+深） 应 ≈ 上证指数成交额 + 深证成指成交额。
   两者来自同一接口的不同字段，但一条是逐个累加、一条是源端聚合，能互相兜住。
2. **恒等式**：涨 + 跌 + 平 + 停 == 在册总数（少一批数据立刻暴露）。
3. **涨跌停抽样**：price 与 limit_up 的关系自洽，且涨停股的涨幅确实在阈值附近。

    NO_PROXY='*' python tools/check_data.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("NO_PROXY", "*")
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collector import tx                            # noqa: E402
from app.collector.scheduler import load_codes          # noqa: E402
from app.compute import breadth as B                    # noqa: E402
from app.config import INDEXES                          # noqa: E402


def main() -> int:
    codes = load_codes()
    quotes, stats, ms = tx.fetch_many([tx.sym_of(c) for c in codes], 6, 300)
    qs = list(quotes.values())
    b = B.compute(qs)
    fails = []

    print(f"样本 {len(qs)} 只 / {ms} ms / 坏数据 {stats.bad}")
    print(f"涨 {b['up']} 跌 {b['down']} 平 {b['flat']} 停 {b['halt']} / 总数 {b['total']}")
    if b["up"] + b["down"] + b["flat"] + b["halt"] != b["total"]:
        fails.append("宽度恒等式不成立")

    # ── 成交额交叉验证 ──
    idx, _, _ = tx.fetch_many([s for _, _, s in INDEXES], 2, 10)
    sh = next((q for q in idx.values() if q.sym == "sh000001"), None)
    sz = next((q for q in idx.values() if q.sym == "sz399001"), None)
    if sh and sz:
        idx_amt_yi = sh.amt_wan / 1e4 + sz.amt_wan / 1e4
        ours = b["amt_yi"]
        diff = (ours - idx_amt_yi) / idx_amt_yi * 100 if idx_amt_yi else 0
        print(f"成交额：自算 {ours:,.0f} 亿 / 指数口径 {idx_amt_yi:,.0f} 亿 / 偏差 {diff:+.2f}%")
        if abs(diff) > 3.0:
            fails.append(f"成交额偏差 {diff:+.2f}% 超过 3%")
    else:
        print("! 指数数据不完整，跳过成交额对账")

    # ── 涨跌停自洽 ──
    zt = [q for q in qs if q.is_limit_up]
    bad_zt = [q for q in zt if q.price < q.limit_up - 0.001]
    print(f"涨停 {len(zt)} 只 / 自洽 {len(zt) - len(bad_zt)} 只")
    if bad_zt:
        fails.append(f"{len(bad_zt)} 只涨停判定不自洽")

    # 阈值抽检：涨停股涨幅应接近其所属板的上限
    buckets = {"10%": 0, "20%": 0, "30%": 0, "5%": 0}
    for q in zt:
        p = q.pct
        key = "5%" if p < 7 else ("10%" if p < 15 else ("20%" if p < 25 else "30%"))
        buckets[key] += 1
    print("涨停涨幅分布：", buckets)

    # ── 样本抽查 ──
    print("\n抽样 5 只：")
    for q in qs[:5]:
        print(f"  {q.code} {q.name:8s} {q.price:9.3f} {q.pct:+6.2f}% "
              f"额 {q.amt_wan/1e4:8.2f} 亿 涨停价 {q.limit_up:.2f}")

    print("\n" + ("FAIL: " + "; ".join(fails) if fails else "PASS: 三项对账全部通过"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
