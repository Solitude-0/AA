# -*- coding: utf-8 -*-
"""热路径数据模型。

刻意用 dataclass(slots=True) 而不是 ORM：
- 全市场 5000+ 行每秒覆盖写，ORM 的实例开销没有意义
- slots 比普通 dict 省 40%~50% 内存，属性访问也更快
"""
from dataclasses import dataclass, field


@dataclass(slots=True)
class Quote:
    """单只标的的实时快照（来源：腾讯 qt.gtimg.cn）。"""

    code: str
    name: str
    sym: str
    price: float
    prev: float
    open: float
    high: float
    low: float
    chg: float
    pct: float
    vol_hand: int            # 成交量（手）
    amt_wan: float           # 成交额（万元）
    turnover: float          # 换手率 %
    vol_ratio: float         # 量比
    amplitude: float         # 振幅 %
    pe: float | None
    pb: float | None
    total_cap_yi: float      # 总市值（亿元）
    float_cap_yi: float      # 流通市值（亿元）
    limit_up: float
    limit_dn: float
    out_vol: int             # 外盘（手）
    in_vol: int              # 内盘（手）
    bid1: float
    bid1_vol: int
    ask1: float
    ask1_vol: int
    bids: tuple[tuple[float, int], ...] = ()   # 五档买（价, 量）
    asks: tuple[tuple[float, int], ...] = ()   # 五档卖（价, 量）
    src_ts: str = ""         # 源端时间戳 YYYYMMDDHHMMSS
    lag_sec: float = 0.0     # 源端时间戳与本机的差

    @property
    def is_halt(self) -> bool:
        """停牌判定：无价且无成交。"""
        return self.price <= 0 and self.vol_hand <= 0

    @property
    def is_limit_up(self) -> bool:
        return self.limit_up > 0 and self.price >= self.limit_up - 0.001

    @property
    def is_limit_down(self) -> bool:
        return self.limit_dn > 0 and self.price <= self.limit_dn + 0.001

    @property
    def outer_inner_ratio(self) -> float | None:
        """内外盘比 = 外盘 / 内盘，衡量主动买卖力度。"""
        return round(self.out_vol / self.in_vol, 3) if self.in_vol > 0 else None

    def diff_against(self, prev: "Quote | None") -> dict:
        """与上一轮快照比对，供异动规则使用。"""
        if prev is None:
            return {}
        return {
            "price": round(self.price - prev.price, 3),
            "pct": round(self.pct - prev.pct, 3),
            "vol_ratio": round(self.vol_ratio - prev.vol_ratio, 3),
            "limit_up_lost": prev.is_limit_up and not self.is_limit_up,
            "limit_dn_lost": prev.is_limit_down and not self.is_limit_down,
        }


@dataclass(slots=True)
class ParseStats:
    """一轮解析的自检结果。任何异常都要能在这里看见。"""

    total: int = 0
    ok: int = 0
    bad_price: int = 0        # 价格三元组不自洽
    bad_pct: int = 0          # 涨跌幅越界
    bad_amount: int = 0       # 成交额与量级不符
    bad_field: int = 0        # 字段数不足 / 转换失败
    halted: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def bad(self) -> int:
        return self.bad_price + self.bad_pct + self.bad_amount + self.bad_field

    @property
    def bad_rate(self) -> float:
        return round(self.bad / self.total, 5) if self.total else 0.0

    def as_dict(self) -> dict:
        return {
            "total": self.total, "ok": self.ok, "bad": self.bad,
            "bad_rate": self.bad_rate, "halted": self.halted,
            "detail": {"price": self.bad_price, "pct": self.bad_pct,
                       "amount": self.bad_amount, "field": self.bad_field},
            "messages": self.messages[:5],
        }
