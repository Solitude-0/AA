# -*- coding: utf-8 -*-
"""延迟 vs 请求频率 实验。

要回答一个问题：**提高刷新频率，数据会不会更新鲜？**

背景：同一天三次测量，延迟差异极大
    10:54   lag ≈ 2~3 s
    11:12   lag ≈ 5.4 s
    11:38   lag ≈ 22 s（当时累计已发出数百次请求）
这看起来像"请求越密集，源端返回的快照越旧"的负反馈。

方法：静默一段时间 → 测一次基线 → 连续轰炸 → 再测 → 再静默 → 复测。

用法：
    NO_PROXY='*' python tools/bench_latency.py --quiet 90 --burst 8
"""
import argparse
import os
import statistics
import sys
import time

os.environ.setdefault("NO_PROXY", "*")
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collector import tx    # noqa: E402

PROBE = ["sh600519", "sz000001", "sh601318", "sh688981", "sz300750"]


def probe_once(tag: str) -> dict:
    """单批 5 只，测 lag 与耗时（请求量最小，最能反映源端真实延迟）。"""
    quotes, stats, ms = tx.fetch_many(PROBE)
    lags = [q.lag_sec for q in quotes.values() if q.lag_sec > 0]
    med = statistics.median(lags) if lags else -1
    print(f"  {tag:<22} 延迟 {med:>6.1f} s   耗时 {ms:>7.1f} ms   命中 {len(quotes)}/5")
    return {"tag": tag, "lag": med, "ms": ms, "n": len(quotes)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", type=float, default=90, help="首轮静默秒数")
    ap.add_argument("--burst", type=int, default=8, help="轰炸轮数")
    a = ap.parse_args()

    print("=" * 66)
    print("阶段 1｜静默后基线（最小请求量）")
    print(f"  等待 {a.quiet:.0f}s 让源端限流窗口复位 ...")
    time.sleep(a.quiet)
    base = [probe_once(f"基线-{i + 1}") for i in range(2)]

    print("\n阶段 2｜连续轰炸（每轮全市场 18 批）")
    codes = ["600519", "000001", "601318", "688981", "300750"]
    syms = [tx.sym_of(c) for c in codes]
    full = []
    for i in range(a.burst):
        q, st, ms = tx.fetch_many(syms + [tx.sym_of(f"{n:06d}") for n in range(1, 200)])
        lags = [x.lag_sec for x in q.values() if x.lag_sec > 0]
        med = statistics.median(lags) if lags else -1
        print(f"  轰炸第 {i + 1:>2} 轮           延迟 {med:>6.1f} s   耗时 {ms:>7.1f} ms   命中 {len(q)}")
        full.append(med)
        time.sleep(0.3)

    print("\n阶段 3｜轰炸刚结束时立即复测")
    after = [probe_once(f"轰炸后-{i + 1}") for i in range(2)]

    print("\n阶段 4｜再静默 45 s 复测")
    time.sleep(45)
    recover = [probe_once(f"恢复-{i + 1}") for i in range(2)]

    print("=" * 66)
    b = statistics.median([x["lag"] for x in base])
    af = statistics.median([x["lag"] for x in after])
    rc = statistics.median([x["lag"] for x in recover])
    print(f"基线延迟   : {b:>6.1f} s")
    print(f"轰炸后延迟 : {af:>6.1f} s   （变化 {af - b:+.1f} s）")
    print(f"静默恢复后 : {rc:>6.1f} s   （相对基线 {rc - b:+.1f} s）")
    print("=" * 66)
    if af - b > 3:
        print("结论：延迟随请求密度上升 → 提高刷新频率会让数据更旧，不是更新鲜。")
        print("      节拍应按「不触发降级」来定，而不是按「理论最快」来定。")
    else:
        print("结论：本次未观察到明显降级。可能当前处于限流窗口之外，")
        print("      建议在不同时段复测后再定节拍。")


if __name__ == "__main__":
    main()
