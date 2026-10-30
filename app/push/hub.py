# -*- coding: utf-8 -*-
"""推送中心：订阅集 + 50 ms 合帧 + 慢连接丢弃（方案 §7）。

三条硬规则，缺一条就会出难查的 bug：
1. **合帧 50 ms**：不同采集任务在同一窗口的变化合并成一帧，否则前端一秒重渲染 4~5 次
2. **断线必全量**：重连后先发 `full:true`，只发增量会因中间漏帧让价格永远停在旧值
3. **慢连接丢弃**：每连接 Queue(maxsize=50)，满则丢最旧帧——宁可缺一帧，
   也不能让一个卡住的浏览器拖慢整个事件循环
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from . import protocol as P

FRAME_MS = 0.05          # 合帧窗口
QUEUE_MAX = 50
HEARTBEAT_SEC = 15.0


class Client:
    __slots__ = ("id", "ws", "queue", "subs", "dropped", "sent", "sender")

    def __init__(self, cid: int, ws) -> None:
        self.id = cid
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.subs: dict[str, set[str]] = {}     # channel -> codes（全量频道用 {"*"}）
        self.dropped = 0
        self.sent = 0
        self.sender: asyncio.Task | None = None

    def wants(self, ch: str, code: str | None = None) -> bool:
        s = self.subs.get(ch)
        if not s:
            return False
        if code is None or "*" in s:
            return True
        return code in s


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._pending: dict[str, dict[str, Any]] = {}
        self._full: dict[str, Any] = {}
        self._handle: asyncio.TimerHandle | None = None
        self._seq = 0
        self.frames = 0
        self.bytes = 0
        self.dropped = 0

    # ── 连接生命周期 ────────────────────────────────────
    async def register(self, ws) -> Client:
        self._seq += 1
        c = Client(self._seq, ws)
        c.sender = asyncio.create_task(self._sender(c))
        self.clients.add(c)
        return c

    def unregister(self, c: Client) -> None:
        self.clients.discard(c)
        if c.sender and not c.sender.done():
            c.sender.cancel()

    def subscribe(self, c: Client, ch: str, codes: list[str] | None = None) -> None:
        if ch not in P.CHANNELS:
            return
        s = c.subs.setdefault(ch, set())
        if ch in ("quote", "index", "tick"):
            for code in (codes or []):
                s.add(str(code))
        else:
            s.add("*")          # 板块/宽度/资金流/异动：整频道

    def unsubscribe(self, c: Client, ch: str, codes: list[str] | None = None) -> None:
        s = c.subs.get(ch)
        if not s:
            return
        if not codes:
            c.subs.pop(ch, None)
            return
        for code in codes:
            s.discard(str(code))
        if not s:
            c.subs.pop(ch, None)

    # ── 发送 ────────────────────────────────────────────
    async def _sender(self, c: Client) -> None:
        try:
            while True:
                text = await c.queue.get()
                await c.ws.send_text(text)
                c.sent += 1
        except (asyncio.CancelledError, Exception):     # noqa: BLE001
            return

    def send_now(self, c: Client, text: str) -> None:
        """直接入队。满了丢最旧——盯盘要"最新一帧"，不是"完整历史"。"""
        try:
            c.queue.put_nowait(text)
        except asyncio.QueueFull:
            try:
                c.queue.get_nowait()
                c.queue.put_nowait(text)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            c.dropped += 1
            self.dropped += 1

    def broadcast(self, text: str, ch: str, code: str | None = None) -> None:
        self.frames += 1
        self.bytes += len(text)
        for c in self.clients:
            if c.wants(ch, code):
                self.send_now(c, text)

    # ── 发布 ────────────────────────────────────────────
    def publish_diff(self, ch: str, items: dict[str, dict]) -> None:
        """累计到合帧缓冲。同名 key 只保留最新值。"""
        if not items:
            return
        buf = self._pending.setdefault(ch, {})
        buf.update(items)
        self._schedule()

    def publish(self, ch: str, data, *, full: bool = False) -> None:
        """整块数据（板块 / 宽度 / 资金流 / 异动）。同频道同帧周期内只留最后一份。"""
        self._full[ch] = (data, full)
        self._schedule()

    def publish_now(self, ch: str, data) -> None:
        """不走合帧，立即广播。

        板块三维每类各一帧（行业/概念/地域），如果走 publish 会被下一次覆盖——
        `_full[ch]` 只保留最后一份，这是个很容易漏的坑。
        """
        self.broadcast(P.frame(ch, data), ch)

    def _schedule(self) -> None:
        if self._handle is not None and not self._handle.cancelled():
            return
        loop = asyncio.get_event_loop()
        self._handle = loop.call_later(FRAME_MS, self._flush)

    def _flush(self) -> None:
        self._handle = None
        pending, self._pending = self._pending, {}
        full, self._full = self._full, {}

        for ch, items in pending.items():
            if not items:
                continue
            # 按订阅分组，避免给每个连接都 json 一遍全量
            payload_sets: dict[frozenset, str] = {}
            for c in self.clients:
                codes = c.subs.get(ch)
                if not codes:
                    continue
                if "*" in codes:
                    chosen = items
                else:
                    chosen = {k: v for k, v in items.items() if k in codes}
                if not chosen:
                    continue
                sig = frozenset(chosen)
                if sig not in payload_sets:
                    payload_sets[sig] = P.frame(ch, list(chosen.values()))
                self.send_now(c, payload_sets[sig])
                self.frames += 1
                self.bytes += len(payload_sets[sig])

        for ch, (data, is_full) in full.items():
            text = P.frame(ch, data, full=is_full)
            self.frames += 1
            self.bytes += len(text)
            for c in self.clients:
                if c.wants(ch):
                    self.send_now(c, text)

    # ── 心跳 ────────────────────────────────────────────
    async def heartbeat(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SEC)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            text = P.hb()
            for c in list(self.clients):
                self.send_now(c, text)

    def stats(self) -> dict:
        return {
            "clients": len(self.clients),
            "frames": self.frames,
            "kb": round(self.bytes / 1024, 1),
            "avg_frame_b": round(self.bytes / max(self.frames, 1), 1),
            "dropped": self.dropped,
            "queue_max": QUEUE_MAX,
            "subs": {c.id: {k: (len(v) if "*" not in v else -1) for k, v in c.subs.items()}
                     for c in self.clients},
        }


HUB = Hub()
