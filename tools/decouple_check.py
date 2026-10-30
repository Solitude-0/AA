# -*- coding: utf-8 -*-
"""独立性护栏：本项目不得引用任何外部本地项目（测试\\webapp、测试\\.build）。

背景：2026-10-23 之前，config.py 的股票清单默认读 测试\\.build\\codes.json，
前端还挂着跳 :8767（旧选股系统）的外链，web\\parts\\kline.js 是从 .build 整份复制的。
这些都是"删掉别的项目就跑不起来 / 改坏别的项目就受影响"的隐患，已全部切断。
本脚本把它们钉死，防止以后手滑再引回来。

用法：
    python tools/decouple_check.py            # 退出码 0 = 干净，1 = 有命中
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 命中即失败：任何一条都意味着本项目又被外部项目绑住了
BAN = [
    (r"测试[\\/]+webapp", "引用旧选股系统目录 测试\\webapp"),
    (r"测试[\\/]+\.build", "引用旧看板构建目录 测试\\.build"),
    (r"workbuddy_work[\\/]+测试", "引用 测试 目录下的任何东西"),
    (r":8767\b", "引用旧服务端口 8767"),
    (r"\.build[\\/]+parts", "引用 .build\\parts 组件"),
    (r"daily_bar", "引用外部表 daily_bar"),
]

SCAN_EXT = {".py", ".js", ".html", ".css", ".md", ".txt", ".bat", ".vbs",
            ".json", ".sql", ".cfg", ".toml", ".yml", ".yaml"}

SKIP_DIR = {".git", ".venv", "__pycache__", "node_modules", ".scratch",
            "state", "logs", ".workbuddy", ".tmp"}


def main() -> int:
    hits: list[tuple[str, int, str, str]] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIR for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() not in SCAN_EXT:
            continue
        if p.name == Path(__file__).name:      # 规则文本本身不算命中
            continue
        try:
            text = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat, why in BAN:
                if re.search(pat, line):
                    hits.append((str(p.relative_to(ROOT)), lineno, why,
                                 line.strip()[:110]))

    if not hits:
        n = sum(1 for p in ROOT.rglob("*")
                if p.is_file() and p.suffix.lower() in SCAN_EXT
                and not any(x in SKIP_DIR for x in p.relative_to(ROOT).parts))
        print(f"[decouple] 干净。扫描 {n} 个文件，0 处外部项目引用。")
        return 0

    print(f"[decouple] 发现 {len(hits)} 处外部项目引用：")
    for f, ln, why, txt in hits:
        print(f"  ✗ {f}:{ln}  [{why}]")
        print(f"      {txt}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
