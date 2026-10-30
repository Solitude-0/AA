# -*- coding: utf-8 -*-
"""收盘作业（15:05，可手动重跑）。

    python -m app.jobs.close            # 立即执行一次
    python -m app.jobs.close --roll     # 只滚动分区

做三件事：
1. 落最后一笔全市场分钟快照 + 板块分钟
2. 抓龙虎榜入库（T+1 数据，接口按 TRADE_DATE 倒序，取最新一页即覆盖最近一日）
3. 预建未来 2 天分区、删除过期分区

全部幂等：重复执行不会产生重复行（lhb 用 ON CONFLICT 更新）。
"""
import argparse
import json
import sys

from ..collector.scheduler import SCHED
from ..db import DB


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roll", action="store_true", help="只做分区滚动")
    ap.add_argument("--no-lhb", action="store_true", help="跳过龙虎榜")
    a = ap.parse_args()

    if not DB.ensure_schema():
        print(f"[!] 数据库不可用: {DB.err}")
        return 1

    if a.roll:
        print(json.dumps(DB.roll_partitions(), ensure_ascii=False))
        return 0

    if a.no_lhb:
        quotes = SCHED.store.snapshot_quotes()
        boards = SCHED.store.snapshot_boards()
        print(f"写入快照 {DB.write_snapshot(quotes, boards)} 行")
        print(json.dumps(DB.roll_partitions(), ensure_ascii=False))
        return 0

    out = SCHED.run_close_job()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
