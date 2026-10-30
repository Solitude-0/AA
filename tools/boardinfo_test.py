# -*- coding: utf-8 -*-
"""boardinfo 离线单测 —— 建议/面板/归属索引，不依赖行情源。

monkeypatch 点位备忘（改代码前先看）：
- build_index 的 members_fetch 是「调用时查全局」，patch boardinfo._members_of 生效；
- panel 同理 patch boardinfo._fetch_members；
- advice 里的分时走 picker._bars、日线走 kline.fetch —— patch 模块属性后必须还原，
  否则污染同进程的其它测试。
"""
import sys
import types
from pathlib import Path

import faulthandler  # noqa: E402 — 30 s 挂起自动转储全部线程栈并退出（排查非确定性卡死）
faulthandler.dump_traceback_later(30, exit=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.compute import boardinfo, picker as picker_mod  # noqa: E402

fails = []


def ck(name, cond, got=None):
    if cond:
        print(f"  ok   {name}")
    else:
        fails.append(f"{name} → {got!r}")
        print(f"  FAIL {name} → {got!r}")


class Q(types.SimpleNamespace):
    """带真实涨停判定的假 Quote（只带谓词会碰的字段）。"""

    @property
    def is_limit_up(self):
        return self.limit_up > 0 and self.price >= self.limit_up - 0.001

    @property
    def is_halt(self):
        return self.price <= 0 and getattr(self, "vol_hand", 1) <= 0


def q(code="600001", name="测试股", price=10.0, prev=10.0, open_=10.2, pct=2.0,
      vr=2.0, out=600, in_=300, limit_up=10.9, high=10.5, low=10.1,
      amt_wan=8000, vol_hand=80000):
    return Q(code=code, name=name, price=price, prev=prev, open=open_, pct=pct,
             vol_ratio=vr, out_vol=out, in_vol=in_, limit_up=limit_up,
             high=high, low=low, amt_wan=amt_wan, vol_hand=vol_hand,
             turnover=5.0, amplitude=3.0, is_st=False, is_limit_down=False)


class FakeStore:
    def __init__(self, quotes, boards=None, flows=None):
        self._q = quotes
        self._b = {b.code: b for b in (boards or [])}
        self._f = flows or {}

    def get(self, c):
        return self._q.get(c)

    def board(self, c):
        return self._b.get(c)

    def flow_of(self, c):
        return self._f.get(c)

    def snapshot_boards(self, kind=None):
        return list(self._b.values())


# ── 1. advice：A 层命中 / 判定档位 / 纪律位 ─────────────────────
# 分数自洽核算（照 _score 公式）：pct10→强度20，vr3.5→量能20，
# 外盘占比0.8→承接20（(0.8-0.5)/0.2×20 封顶），双命中→形态20，合计 80 ≥ 75
store = FakeStore({
    "600001": q(price=10.8, prev=10.0, open_=10.3, pct=10.0,
                vr=3.5, out=800, in_=200, limit_up=11.0, high=10.9),  # gap+order 双命中
    "600002": q("600002", price=9.8, prev=10.0, open_=9.9, pct=-2.0,
                vr=0.5, out=100, in_=900, limit_up=11.0),  # 下跌弱票
})
old_bars, old_fetch = picker_mod._bars, boardinfo.kline.fetch
picker_mod._bars = lambda c: []                          # 分时取不到
boardinfo.kline.fetch = lambda *a, **k: {"bars": []}      # 日线取不到
try:
    a = boardinfo.advice(store, "600001")
    ck("建议 ok", a["ok"] is True, a)
    ck("gap+order 双命中", sorted(a["hits"]) == ["gap", "order"], a["hits"])
    ck("B 层模式如实不命中（gate 或 无分时）", all(
        m["hit"] is False for m in a["modes"] if m["lv"] == "B"), a["modes"])
    ck("无分时有诚实备注", any("分时未取到" in n for n in a["notes"]), a["notes"])
    ck("可参与档（score≥75 且命中）", a["verdict"] == "可参与", (a["verdict"], a["score"]))
    ck("纪律含开盘价", any("开盘价" in s for s in a["stops"]), a["stops"])

    a2 = boardinfo.advice(store, "600002")
    ck("下跌票提示不接飞刀", any("不接飞刀" in n for n in a2["notes"]), a2["notes"])
    ck("下跌弱票不给「可参与」", a2["verdict"] != "可参与", a2["verdict"])
finally:
    picker_mod._bars, boardinfo.kline.fetch = old_bars, old_fetch

# 快照缺票
a3 = boardinfo.advice(store, "999999")
ck("快照外返回 ok=False", a3["ok"] is False, a3)

# ── 2. _swing_levels ───────────────────────────────────────────
# 数值全部互异，避免「平台化高点」在 ≤ 比较下全成摆动点的退化情形
_h = [10.0, 10.1, 10.4, 12.0, 10.4, 10.1, 9.5, 9.0, 9.5, 10.0]
_l = [9.9, 9.95, 10.2, 11.8, 10.2, 9.95, 9.3, 8.8, 9.3, 9.9]
bars = [{"o": 10, "c": c, "h": h, "l": lv}
        for c, h, lv in zip([10, 10.05, 10.3, 11.9, 10.3, 10.0, 9.4, 9.0, 9.4, 9.95], _h, _l)]
r1, s1 = boardinfo._swing_levels(bars, 10.0)
ck("压力取摆动高点上方", r1 == 12.0, r1)
ck("支撑取摆动低点下方", s1 == 8.8, s1)
r2, s2 = boardinfo._swing_levels(bars[:6], 10.0)          # 根数不足
ck("少于 10 根返回空", r2 is None and s2 is None, (r2, s2))

# ── 3. panel：梯队 / 样本资金流 / 强度 ─────────────────────────
boards = [types.SimpleNamespace(code="new_xx", name="测试板块", kind="i",
                                pct=2.5, src="sina", cnt=4, net_in=0.0,
                                leader_name="甲", leader_code="600001")]
store2 = FakeStore(
    {"600001": q("600001", name="甲", price=10.9, limit_up=10.9),   # 真涨停
     "600002": q("600002", name="乙")},
    boards=boards,
    flows={"600002": {"net": 2.5}})
members = [
    {"c": "600001", "n": "甲", "p": 10.9, "pc": 9.9, "amt": 1.0, "to": 1, "cap": 10},
    {"c": "600002", "n": "乙", "p": 10.2, "pc": 2.0, "amt": 1.0, "to": 1, "cap": 10},
    {"c": "600003", "n": "丙", "p": 10.1, "pc": 9.9, "amt": 1.0, "to": 1, "cap": 10},   # 不在快照 → 粗判
    {"c": "600004", "n": "丁", "p": 9.5, "pc": -5.0, "amt": 1.0, "to": 1, "cap": 10},
]
old_fm = boardinfo._fetch_members
boardinfo._fetch_members = lambda bk, max_members=400: members   # panel 只要 list
try:
    p = boardinfo.panel(store2, "new_xx")
    ck("面板 ok", p["ok"] is True, p)
    ck("梯队 涨停=2（1 真 + 1 粗判）", p["ladder"]["zt"] == 2, p["ladder"])
    ck("粗判单独计数", p["ladder"]["rough_zt"] == 1, p["ladder"])
    ck("下跌档=1", p["ladder"]["dn"] == 1, p["ladder"])
    ck("样本资金流覆盖 1/4", p["flow"]["hit"] == 1 and p["flow"]["cov"] == 25, p["flow"])
    ck("样本净额=2.5", p["flow"]["net"] == 2.5, p["flow"]["net"])
    ck("新浪板块持续性如实 na", p["persist"]["ok"] is False and "新浪" in p["persist"]["note"], p["persist"])
    ck("强度分量带阈值说明", len(p["strength"]["items"]) == 3 and all(
        "rule" in it for it in p["strength"]["items"]), p["strength"])
finally:
    boardinfo._fetch_members = old_fm

# BK 板块持续性（patch kline.fetch）
boardinfo._fetch_members = lambda bk, max_members=400: members[:1]   # panel 只要 list
em_board = types.SimpleNamespace(code="BK0475", name="银行", kind="i", pct=1.0,
                                 src="em", cnt=40, net_in=5.0,
                                 leader_name="甲", leader_code="600001")
store3 = FakeStore({"600001": q()}, boards=[em_board])
day_bars = [{"o": 10, "c": 10 + i * 0.2, "h": 10.5, "l": 9.9} for i in range(8)]
boardinfo.kline.fetch = lambda *a, **k: {"bars": day_bars}
try:
    p2 = boardinfo.panel(store3, "BK0475")
    ck("BK 板块持续性可算", p2["persist"]["ok"] is True, p2["persist"])
    ck("近5日上涨 → 延续", p2["persist"]["label"] == "延续", p2["persist"])
finally:
    boardinfo._fetch_members = old_fm
    boardinfo.kline.fetch = old_fetch

# ── 4. 归属索引构建 ────────────────────────────────────────────
import tempfile  # noqa: E402
tmp = Path(tempfile.mkdtemp()) / "board_index.json"
boardinfo.INDEX_PATH = tmp
boards4 = [
    types.SimpleNamespace(code="new_a", name="板块A", kind="i", pct=1.0),
    types.SimpleNamespace(code="gn_b", name="板块B", kind="c", pct=2.0),
]
members_by_bk = {
    "new_a": [{"c": "600001", "n": "甲"}, {"c": "000002", "n": "乙"}],
    "gn_b": [{"c": "600001", "n": "甲"}, {"c": "300003", "n": "丙"}],
}
old_mo = boardinfo._members_of
boardinfo._members_of = lambda bk, max_members=400: (members_by_bk[bk], len(members_by_bk[bk]))
try:
    meta = boardinfo.build_index(boards4)
    ck("索引完成", meta["done"] is True and meta["n_boards"] == 2, meta)
    bl, st = boardinfo.boards_of(FakeStore({}), "600001")
    ck("600001 归属两类", {b["c"] for b in bl} == {"new_a", "gn_b"}, bl)
    ck("索引状态 stocks=3", st["stocks"] == 3, st)
    ck("缓存落盘", tmp.exists(), tmp)
    bl2, _ = boardinfo.boards_of(FakeStore({}), "000002")
    ck("000002 只属 A", [b["c"] for b in bl2] == ["new_a"], bl2)
finally:
    boardinfo._members_of = old_mo

# ── 源派发：BK 码走东财，其余走新浪 ─────────────────────────
# 2026-10-26 的 bug：_members_of 无脑走新浪。东财被封期间板块源本来就是新浪，
# 看不出来；东财一恢复（板块码变 BKxxxx），1033 类全查空，归属索引变成空壳。
from app.collector import em, sina  # noqa: E402

calls = {"em": 0, "sina": 0}


def _fake_em(bk, page=1, size=50):
    calls["em"] += 1
    return [{"c": "600001", "n": "甲"}], 1


def _fake_sina(node, page=1, size=50):
    calls["sina"] += 1
    return [{"c": "000002", "n": "乙"}], 1


old_em, old_sina = em.fetch_board_members, sina.fetch_board_members
em.fetch_board_members, sina.fetch_board_members = _fake_em, _fake_sina
try:
    rows, _t = boardinfo._members_of("BK1565")
    ck("BK 码走东财（新浪一次都不该调）",
       calls["em"] == 1 and calls["sina"] == 0 and rows and rows[0]["c"] == "600001",
       calls)
    calls["em"] = calls["sina"] = 0
    rows2, _t2 = boardinfo._members_of("new_xx")
    ck("新浪节点走新浪（东财一次都不该调）",
       calls["sina"] == 1 and calls["em"] == 0 and rows2 and rows2[0]["c"] == "000002",
       calls)
finally:
    em.fetch_board_members, sina.fetch_board_members = old_em, old_sina

print()
print("FAIL:", len(fails))
for f in fails:
    print("  -", f)
print("BOARDINFO_ALL_PASS" if not fails else "BOARDINFO_FAIL")
sys.exit(1 if fails else 0)
