# -*- coding: utf-8 -*-
"""rank.build_filter 离线单测 —— 不依赖行情源。

动机：市场板块筛选（boards）动的是全市场 5343 只的过滤谓词，错了影响每一页。
端到端验证只能证明「当前页没混入」，证明不了分页总数自洽与边界前缀
（920xxx 北证、689 科创板、43xxxx 北证）都判对 —— 这些用构造数据直接打。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.compute.rank import build_filter, query  # noqa: E402

fails = []


def ck(name, cond, got=None):
    if not cond:
        fails.append(f"{name} → {got!r}")
        print(f"  FAIL {name} → {got!r}")
    else:
        print(f"  ok   {name}")


def q(code, name="测试", pct=1.0, amt_wan=10_000, **kw):
    """构造一个只带谓词会读到的字段的假 Quote。"""
    base = dict(
        code=code, name=name, pct=pct, amt_wan=amt_wan, turnover=5.0,
        vol_ratio=2.0, amplitude=3.0, is_halt=False, is_st=False,
        is_limit_up=False, is_limit_down=False,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


# ── 市场映射的边界前缀 ──────────────────────────────────────────
MARKET_CASES = [
    ("600519", "sh"), ("000001", "sz"), ("300750", "cyb"), ("301269", "cyb"),
    ("688981", "kcb"), ("689009", "kcb"),          # 689 也是科创板
    ("920091", "bj"), ("830799", "bj"), ("430047", "bj"), ("920xxx".replace("xxx", "100"), "bj"),
]
for code, want in MARKET_CASES:
    got_market = None
    rows, _ = query([q(code)], size=10, flt={"boards": [want]})
    ck(f"market {code}∈{want}", len(rows) == 1, rows)

# ── boards 组合与排除 ──────────────────────────────────────────
pool = [q(c) for c in ("600519", "000001", "300750", "688981", "920091")]
rows, total = query(pool, size=10, flt={"boards": ["cyb", "bj"]})
ck("cyb+bj 恰好 2 只", total == 2 and {r.code for r in rows} == {"300750", "920091"}, (total, rows))

rows, total = query(pool, size=10, flt={"boards": []})
ck("boards 空 = 不过滤", total == 5, total)

rows, total = query(pool, size=10, flt={"boards": ["xxx"]})
# 严格语义：未知市场 = 无匹配（给空表暴露错误），而不是悄悄忽略。
# 浏览器侧的垃圾值由 main.py 白名单拦掉，到不了这里。
ck("boards 非法值 = 空结果（严格语义）", total == 0, total)

# ── boards 与其他条件叠加 ──────────────────────────────────────
pool = [
    q("600519", pct=2.0, name="贵州茅台"),
    q("600000", pct=-1.0),
    q("300750", pct=3.0, name="宁德时代"),
    q("300001", pct=0.5),
]
rows, total = query(pool, size=10, flt={"boards": ["cyb"], "pct_min": 1.0})
ck("cyb ∧ 涨幅≥1 恰好 1 只", total == 1 and rows[0].code == "300750", (total, [r.code for r in rows]))

rows, total = query(pool, size=10, flt={"boards": ["sh"], "kw": "茅台"})
ck("sh ∧ 关键词 恰好 1 只", total == 1 and rows[0].code == "600519", (total, [r.code for r in rows]))

rows, total = query(pool, size=10, flt={"boards": ["cyb", "sh"], "pct_min": 1.0})
ck("多市场 ∧ 涨幅 2 只", total == 2, (total, [r.code for r in rows]))

# ── 分页与 boards 叠加（总数不该被分页截断） ─────────────────────
big = [q(f"300{i:03d}", pct=float(i % 7)) for i in range(120)]
rows, total = query(big, size=50, page=1, flt={"boards": ["cyb"]})
ck("120 只 cyb 第 1 页 50 行", len(rows) == 50, len(rows))
ck("total=120（分页不动总数）", total == 120, total)
rows2, _ = query(big, size=50, page=3, flt={"boards": ["cyb"]})
ck("第 3 页 20 行", len(rows2) == 20, len(rows2))
ck("三页无重叠", not ({r.code for r in rows} & {r.code for r in rows2}), "")

# ── 原有条件回归：halt / ST / 涨停 ─────────────────────────────
pool = [q("600001", is_halt=True), q("600002", is_st=True), q("300001", is_limit_up=True)]
rows, total = query(pool, size=10, flt={})
ck("默认排除停牌", all(r.code != "600001" for r in rows), [r.code for r in rows])
rows, total = query(pool, size=10, flt={"excl_st": True})
ck("排 ST", all(r.code != "600002" for r in rows), [r.code for r in rows])
rows, total = query(pool, size=10, flt={"only_zt": True})
ck("只涨停（可与 boards 叠加）", [r.code for r in rows] == ["300001"], [r.code for r in rows])
rows, total = query(pool, size=10, flt={"only_zt": True, "boards": ["sh"]})
ck("只涨停 ∧ sh = 空", total == 0, total)

print()
print("FAIL:", len(fails))
for f in fails:
    print("  -", f)
print("RANK_FILTER_ALL_PASS" if not fails else "RANK_FILTER_FAIL")
sys.exit(1 if fails else 0)
