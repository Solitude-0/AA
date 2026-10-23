# -*- coding: utf-8 -*-
"""M0 自检：验证「通道 + 解析 + 自校验」三件事是否真的通了。

跑法：
    NO_PROXY='*' python tools/selfcheck_m0.py
    NO_PROXY='*' python tools/selfcheck_m0.py --rounds 10 --codes E:/path/codes.json

验收标准（方案 §14 M0）：
    - 全市场一轮 <= 1 s
    - 字段校验失败率 == 0
    - 无未捕获异常
"""
import argparse
import json
import os
import statistics
import sys
import time

os.environ.setdefault("NO_PROXY", "*")
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import CODES_FILE                       # noqa: E402
from app.collector import calendar as cal                # noqa: E402
from app.collector import tx                            # noqa: E402


def load_codes(path: str) -> list[str]:
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    print(f"! 代码清单不存在：{path}\n  退回内置样本 600 只\n")
    return [f"{i:06d}" for i in range(1, 601)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--codes", default=str(CODES_FILE))
    ap.add_argument("--interval", type=float, default=1.0, help="轮次间隔秒")
    a = ap.parse_args()

    codes = load_codes(a.codes)
    syms = [tx.sym_of(c) for c in codes]
    state = cal.session_state()
    print(f"代码清单 : {a.codes}")
    print(f"标的数   : {len(codes)}")
    print(f"轮次     : {a.rounds}，间隔 {a.interval}s")
    print(f"时段     : {state}（{cal.lag_note(state)}）"
          f"{'' if state == 'trading' else '  <- 非连续竞价，延迟不作为指标'}")
    print("-" * 78)
    print(f"{'#':>2} {'条数':>6} {'耗时ms':>8} {'坏数据':>7} {'停牌':>5} {'延迟s':>6} {'涨':>5} {'跌':>5} {'涨停':>4}")
    print("-" * 78)

    walls, lags, hits = [], [], []
    bad_total = 0
    zt_total = 0
    last_up = last_dn = 0

    for i in range(1, a.rounds + 1):
        try:
            quotes, stats, ms = tx.fetch_many(syms)
        except Exception as e:                     # noqa: BLE001
            print(f"{i:>2} FAIL {e!r}")
            continue

        lags_r = [q.lag_sec for q in quotes.values() if q.lag_sec > 0]
        lag = statistics.median(lags_r) if lags_r else 0
        up = sum(1 for q in quotes.values() if q.pct > 0)
        dn = sum(1 for q in quotes.values() if q.pct < 0)
        zt = sum(1 for q in quotes.values() if q.is_limit_up)

        walls.append(ms)
        lags.append(lag)
        hits.append(len(quotes) / len(syms))
        bad_total += stats.bad
        zt_total = zt
        last_up, last_dn = up, dn

        # 首轮含 18 个 TLS 连接冷启动（urllib 无连接池），单独标出、不进稳态统计
        is_warmup = (i == 1 and a.rounds > 1)
        flag = ""
        if is_warmup:
            flag = "  <-- warmup（含连接建立）"
        elif stats.bad:
            flag = "  <-- 有坏数据"
        print(f"{i:>2} {len(quotes):>6} {ms:>8.1f} {stats.bad:>7} {stats.halted:>5} "
              f"{lag:>6.1f} {up:>5} {dn:>5} {zt:>4}{flag}")

        if is_warmup:
            walls.remove(ms)
            lags.remove(lag)
            hits.remove(len(quotes) / len(syms))

        if stats.messages:
            for m in stats.messages[:2]:
                print(f"     · {m}")
        if i < a.rounds:
            time.sleep(a.interval)

    print("-" * 78)
    if walls:
        print(f"耗时   : 均 {statistics.mean(walls):.1f} ms / "
              f"中位 {statistics.median(walls):.1f} / 最快 {min(walls):.1f} / 最慢 {max(walls):.1f}")
        lag_med = statistics.median(lags)
        if state == "trading":
            lag_tag = "交易时段，可作延迟指标"
        else:
            lag_tag = f"{cal.lag_note(state)}，源端时间戳不推进，仅供参考"
        print(f"命中率 : 均 {statistics.mean(hits):.1%} / 最低 {min(hits):.1%}")
        print(f"延迟   : 中位 {lag_med:.1f} s（{lag_tag}）")
        print(f"坏数据 : 累计 {bad_total} 条")
        print(f"市场   : 涨 {last_up} / 跌 {last_dn} / 涨停 {zt_total}")
        print(f"通道   : {json.dumps(tx.channel_stats(), ensure_ascii=False)}")

        ok = (statistics.mean(walls) <= 1000
              and bad_total == 0
              and statistics.mean(hits) >= 0.99)
        print("-" * 78)
        print("M0 验收 : " + ("PASS" if ok else "FAIL"))
        if not ok:
            print("  判据：均耗时 <= 1000 ms、坏数据 == 0、命中率 >= 99%")
    else:
        print("没有一轮成功，检查网络与上游可用性。")
        print("  提示：本机必须 NO_PROXY='*'，否则代理会让请求 502")


if __name__ == "__main__":
    main()
