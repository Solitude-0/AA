# -*- coding: utf-8 -*-
"""帧协议。字段名极简是刻意的：一帧 500 行时，完整字段名会让体积翻 2.5 倍。"""
from __future__ import annotations

import json
from datetime import datetime

CHANNELS = ("index", "quote", "board", "breadth", "flow", "tick", "alert")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def frame(t: str, data, *, full: bool = False) -> str:
    obj = {"t": t, "ts": now_iso(), "d": data}
    if full:
        obj["full"] = True
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def hello(session: str, lag_sec: float, total: int) -> str:
    return json.dumps({
        "t": "hello", "ts": now_iso(), "session": session,
        "lag_sec": lag_sec, "total": total,
        "channels": list(CHANNELS),
    }, ensure_ascii=False, separators=(",", ":"))


def hb() -> str:
    return json.dumps({"t": "hb", "ts": now_iso()}, separators=(",", ":"))


def err(msg: str) -> str:
    return json.dumps({"t": "err", "msg": msg}, ensure_ascii=False,
                      separators=(",", ":"))
