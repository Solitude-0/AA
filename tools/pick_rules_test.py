# -*- coding: utf-8 -*-
"""B 层四个模式（platform / dragon / ma / tail）的离线单测。

为什么要有这个脚本：这四个形态判定要靠 1m 分时序列，而 push2his 是**间歇性可用**的
（2026-10-23 15:20 之后整个下午 `kline` 通道 17 次请求 0 成功）。
靠线上跑，等于把「B 层逻辑对不对」这个问题押在运气上——绝大多数时候它根本没被调用，
错了也看不见。所以直接手工造分时序列打进去，跟源站好不好没关系。

跑法：  .venv\\Scripts\\python.exe tools/pick_rules_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.compute import picker as P  # noqa: E402
from app.models import Quote  # noqa: E402

FAILS: list[str] = []


def ck(name, cond, got=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s → %s" % (name, got))
        print("  FAIL %s → %s" % (name, got))


def mkq(**kw) -> Quote:
    """造一个 Quote。默认值取「无否定症状」的平庸状态，测试里只覆盖关心的字段。"""
    d = dict(
        code="600000", name="测试", sym="sh600000",
        price=10.0, prev=10.0, open=10.0, high=10.0, low=10.0,
        chg=0.0, pct=0.0, vol_hand=1, amt_wan=1.0, turnover=1.0,
        vol_ratio=1.0, amplitude=1.0, pe=None, pb=None,
        total_cap_yi=100.0, float_cap_yi=100.0,
        limit_up=0.0, limit_dn=0.0,
        out_vol=0, in_vol=0, bid1=10.0, bid1_vol=1, ask1=10.01, ask1_vol=1,
    )
    d.update(kw)
    return Quote(**d)  # type: ignore[arg-type]


def bars_from(prices, t0=(9, 30), vol=None, spread=0.0):
    """按收盘价序列造 1m bar。spread 给每根加上下影线，用来控制振幅。"""
    out = []
    h, m = t0
    for i, c in enumerate(prices):
        out.append({
            "t": "%02d:%02d" % (h, m),
            "o": round(prices[max(0, i - 1)] * 100, 2) / 100,
            "c": c,
            "h": round(c + spread, 4),
            "l": round(c - spread, 4),
            "v": (vol[i] if vol else 100),
            "amt": 0.0,
        })
        m += 1
        if m >= 60:
            m = 0
            h += 1
    return out


print("\n── 共性：没有分时数据时一律不通过 ─────────────────────────")
for mk in ("platform", "dragon", "ma", "tail"):
    ok, why = P._judge_b(mkq(), mk, {}, [])
    ck("bars 为空时 %s 不通过" % mk, ok is False and why == "无分时数据", why)

print("\n── ① 分时平台突破 ───────────────────────────────────────")
P_PARAMS = {"flat_min": 20, "amp_max": 1.2, "vol_x": 1.5}
flat = [10.0] * 59                      # 前 59 根全部横在 10.00
vol = [100] * 59 + [300]
b_ok = bars_from(flat + [10.0], vol=vol, spread=0.02)
q_ok = mkq(price=10.20)
ok, why = P._judge_b(q_ok, "platform", P_PARAMS, b_ok)
ck("横盘+放量突破 → 通过", ok, why)

ok, why = P._judge_b(mkq(price=10.20), "platform", P_PARAMS,
                     bars_from([10.0] * 59 + [10.0], vol=[100] * 60, spread=0.06))
ck("振幅 1.2% 上限被突破 → 不通过", ok is False and "振幅" in why, why)

ok, why = P._judge_b(mkq(price=10.01), "platform", P_PARAMS, b_ok)
ck("未破平台上沿 → 不通过", ok is False and "未破平台上沿" in why, why)

b_novol = bars_from(flat + [10.0], vol=[100] * 59 + [120])
ok, why = P._judge_b(mkq(price=10.20), "platform", P_PARAMS, b_novol)
ck("突破未放量（1.2x < 1.5x）→ 不通过", ok is False and "未放量" in why, why)

ok, why = P._judge_b(mkq(price=10.20), "platform", P_PARAMS,
                     bars_from([10.0] * 20, spread=0.02))
ck("分时不足 25 根 → 不通过", ok is False and "不足" in why, why)

print("\n── ② 涨停龙回头 ─────────────────────────────────────────")
D_PARAMS = {"pullback": 3.0}


def dragon_bars(pre_low, limit_at, n=60):
    """前段在 pre_low 附近，第 limit_at 根摸到 11.0，之后回落到 10.8。"""
    ps = [pre_low] * limit_at + [11.0] + [10.8] * (n - limit_at - 1)
    bs = bars_from(ps, spread=0.0)
    bs[limit_at]["h"] = 11.0
    return bs


q_dragon = mkq(price=10.80, high=11.00, limit_up=11.00)
ok, why = P._judge_b(q_dragon, "dragon", D_PARAMS, dragon_bars(10.50, 30))
ck("摸涨停+回踩 1.8%+未破前低 → 通过", ok, why)

q_deep = mkq(price=10.60, high=11.00, limit_up=11.00)
ok, why = P._judge_b(q_deep, "dragon", D_PARAMS, dragon_bars(10.50, 30))
ck("回调 3.6% 超过 3% 上限 → 不通过", ok is False and "回调" in why, why)

q_broke = mkq(price=10.80, high=11.00, limit_up=11.00)
ok, why = P._judge_b(q_broke, "dragon", D_PARAMS, dragon_bars(10.90, 30))
ck("跌破涨停前分时低点 → 不通过", ok is False and "破涨停前低点" in why, why)

q_notouch = mkq(price=10.30, high=10.50, limit_up=11.00)
ok, why = P._judge_b(q_notouch, "dragon", D_PARAMS, dragon_bars(10.30, 30))
ck("从未触及涨停 → 不通过", ok is False and "未触及涨停" in why, why)

print("\n── ③ 分时均线金叉 ───────────────────────────────────────")
M_PARAMS = {"within": 3}
down = [10.0 - i * 0.02 for i in range(80)]          # 单调下行
# 拉升段必须是**加速**上行。踩过的坑：第一段写成线性（每根 +0.06），
# 结果 DIF 增速递减 → MACD 柱在末根已经回落 → "MACD 未红柱放大"。
# 这是标准 MACD 的正确行为（等速上涨本来就有柱收敛），不是代码错，
# 所以正的测试用例要造加速度，让红柱在最后一根仍在放大。
_step = 0.03
up = []
_p = 8.4
for _i in range(80):
    _p += _step
    _step *= 1.05
    up.append(_p)
b_ma = bars_from(down + up)
ok, why = P._judge_b(mkq(price=up[-1], pct=3.0, vol_ratio=1.5), "ma", M_PARAMS, b_ma)
ck("先跌后拉升 → 金叉成立", ok, why)

ok, why = P._judge_b(mkq(price=down[-1], pct=-3.0, vol_ratio=1.5), "ma", M_PARAMS,
                     bars_from(down + [10.0 - (80 + i) * 0.02 for i in range(80)]))
ck("一路下行 → 不通过（无金叉 / 红柱）", ok is False, why)

print("\n── ④ 尾盘抢筹 ───────────────────────────────────────────")
T_PARAMS = {"after": 3.0, "near_high": 95}
# 14:30 之前平躺 100，14:30 起拉到 104
day = []
h, m = 9, 30
while (h, m) <= (15, 0):
    day.append((h, m))
    m += 1
    if m >= 60:
        m = 0
        h += 1
day = [t for t in day if t >= (9, 30) and (t < (11, 31) or t >= (13, 0))]
times = ["%02d:%02d" % t for t in day]
prices = [100.0 if t < "14:30" else 100.0 + min(4.0, (day.index(i) if False else 0)) for i, t in zip(day, times)]
# 更直白地造：14:30 之前 100，之后每根 +0.08
prices = []
tail_n = len([x for x in times if x >= "14:30"])
for _ti, t in enumerate(times):
    if t < "14:30":
        prices.append(100.0)
    else:
        prices.append(100.0 + 0.15 * ([x for x in times if x >= "14:30"].index(t) + 1))
assert tail_n > 0


def timed_bars(prices, times):
    return [{"t": t, "o": prices[max(0, i - 1)], "c": c, "h": c, "l": c, "v": 100, "amt": 0.0}
            for i, (t, c) in enumerate(zip(times, prices))]


b_tail = timed_bars(prices, times)
ok, why = P._judge_b(mkq(price=prices[-1], pct=3.5, high=prices[-1]), "tail", T_PARAMS, b_tail)
ck("14:30 后拉升 %.1f%% → 通过" % ((prices[-1] / 100.0 - 1) * 100), ok, why)

flat_prices = [100.0 if t < "14:30" else 101.0 for t in times]
ok, why = P._judge_b(mkq(price=101.0, pct=1.0, high=101.0), "tail", T_PARAMS,
                     timed_bars(flat_prices, times))
ck("14:30 后仅涨 1%（<3%）→ 不通过", ok is False and "14:30 后仅" in why, why)

short_times = [t for t in times if t < "14:30"]
ok, why = P._judge_b(mkq(price=101.0), "tail", T_PARAMS,
                     timed_bars([100.0] * len(short_times), short_times))
ck("没有 14:30 之后的数据 → 不通过", ok is False and "无 14:30" in why, why)

# 「尾盘破位」这条分支实际不可达：low_after 取的是 14:30 后所有 bar 的最低价，
# 而 14:30 第一根（锚点）的最低价 ≤ 它的收盘价 ≤ 现价（要满足 after ≥3%），
# 所以 price < low_after 永远不成立。留着不删是因为它防的是 q.price 领先于
# bars 最后一根的极端情况；这里不断言，只记录在案。
print("  注：「尾盘破位」分支当前不可达（锚点最低价 ≤ 收盘价 ≤ 现价），保留作护栏不断言")

print()
if FAILS:
    print("FAIL %d 项:" % len(FAILS))
    for f in FAILS:
        print("  · " + f)
    sys.exit(1)
print("PICK_RULES_ALL_PASS")
