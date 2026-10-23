# -*- coding: utf-8 -*-
"""HTTP 通道：令牌桶 + 域名轮换 + 指数退避 + 熔断。

为什么需要这四个东西（2026-10-23 实测依据）：
- 东财 push2 **裸请求 0/12 成功**，35% 概率直接 RemoteDisconnected
- 3 次退避 + 域名轮换后 **11/12 = 92%**
- 带 Referer 反而 100% 失败（WAF 当前口径），所以请求头只给 User-Agent
- 信号量只限并发不限速率，18 批瞬间打完照样触发限流 → 必须令牌桶

这里用同步实现 + 线程池，M0 阶段够用；
M3 接入 WebSocket 时整体换成 httpx.AsyncClient 即可，接口签名保持不变。
"""
from __future__ import annotations

import random
import threading
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 只给 UA。实测：带上 Referer 会被东财 WAF 直接断连。
BASE_HEADERS = {"User-Agent": UA}


class TokenBucket:
    """线程安全令牌桶。qps 为每秒补充的令牌数，容量 = qps（允许 1 秒的突发）。"""

    def __init__(self, qps: float, capacity: float | None = None):
        self.qps = max(float(qps), 0.1)
        self.capacity = float(capacity if capacity is not None else max(qps, 1))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.waited = 0.0

    def take(self, n: float = 1.0) -> float:
        """取 n 个令牌，不够就阻塞等待。返回实际等待秒数。"""
        spent = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.qps)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    self.waited += spent
                    return spent
                deficit = n - self._tokens
                sleep_for = deficit / self.qps
            time.sleep(min(sleep_for, 0.5))
            spent += min(sleep_for, 0.5)


class CircuitBreaker:
    """连续失败 N 次 → 打开 T 秒。打开期间快速失败，让上层直接用缓存。"""

    def __init__(self, fails: int = 5, seconds: float = 60.0):
        self.fails = fails
        self.seconds = seconds
        self._count = 0
        self._open_until = 0.0
        self._lock = threading.Lock()
        self.trips = 0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    @property
    def remain(self) -> float:
        return max(0.0, self._open_until - time.monotonic())

    def success(self) -> None:
        with self._lock:
            self._count = 0

    def failure(self) -> None:
        with self._lock:
            self._count += 1
            if self._count >= self.fails:
                self._open_until = time.monotonic() + self.seconds
                self._count = 0
                self.trips += 1


class ChannelError(RuntimeError):
    pass


class CircuitOpen(ChannelError):
    pass


class HttpChannel:
    """带节流的 HTTP 通道。

    hosts: 多个域名，重试时轮换（东财不同域名可用性不同，实测 push2 8 / 82.push2 4 / push2delay 1 次命中）
    """

    def __init__(self, hosts: list[str] | tuple[str, ...], qps: float = 10,
                 timeout: float = 6, tries: int = 3,
                 breaker_fails: int = 5, breaker_sec: float = 60):
        self.hosts = list(hosts)
        self.timeout = timeout
        self.tries = tries
        self.bucket = TokenBucket(qps)
        self.breaker = CircuitBreaker(breaker_fails, breaker_sec)
        self.requests = 0
        self.success = 0
        self.host_hits: dict[str, int] = {}

    def fetch(self, path: str, encoding: str = "utf-8") -> bytes:
        """按域名轮换 + 退避重试拉取；全失败抛 ChannelError。"""
        if self.breaker.is_open:
            raise CircuitOpen(f"熔断中，剩余 {self.breaker.remain:.0f}s")

        last: Exception | None = None
        for i in range(self.tries):
            host = self.hosts[i % len(self.hosts)]
            self.bucket.take()
            self.requests += 1
            url = f"https://{host}{path}" if path.startswith("/") else path
            try:
                req = urllib.request.Request(url, headers=BASE_HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                self.success += 1
                self.breaker.success()
                self.host_hits[host] = self.host_hits.get(host, 0) + 1
                return raw
            except Exception as e:            # noqa: BLE001 — 网络异常一律重试
                last = e
                self.breaker.failure()
                # 指数退避 + 抖动，避免多线程同步重试形成尖峰
                time.sleep(0.25 * (2 ** i) + random.uniform(0, 0.15))

        raise ChannelError(f"全部域名失败 after {self.tries} tries: {last!r}") from last

    def get_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.fetch(path, encoding).decode(encoding, "replace")

    def stats(self) -> dict:
        return {
            "requests": self.requests,
            "success": self.success,
            "ok_rate": round(self.success / self.requests, 4) if self.requests else None,
            "breaker_open": self.breaker.is_open,
            "breaker_trips": self.breaker.trips,
            "host_hits": dict(self.host_hits),
            "bucket_waited": round(self.bucket.waited, 2),
        }
