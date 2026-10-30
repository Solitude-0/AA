# -*- coding: utf-8 -*-
"""HTTP 通道：令牌桶 + 域名轮换 + 指数退避 + 熔断。

为什么需要这四个东西（2026-10-23 实测依据）：
- 东财 push2 **裸请求 0/12 成功**，35% 概率直接 RemoteDisconnected
- 3 次退避 + 域名轮换后 **11/12 = 92%**
- 带 Referer 反而 100% 失败（WAF 当前口径），所以请求头只给 User-Agent
- 信号量只限并发不限速率，18 批瞬间打完照样触发限流 → 必须令牌桶

**两条 2026-10-23 下午复测出来的重要结论（推翻了早前"IP 级封禁"的判断）**：
1. `www.eastmoney.com` / `quote.eastmoney.com` / `datacenter-web` / `push2his` 全部 200，
   `push2` 的 `ulist.np/get` 也 200 —— 不是 IP 封禁，是**按路径**的 WAF 规则：
   `api/qt/clist/get`、`fflow/kline/get` 这两个路径对任何请求方式都直接断连（curl 也是 000）。
2. `push2his` 的 kline、`push2` 的 `stock/get` / `stock/details/get` 只认**浏览器 TLS 指纹**：
   同一 URL、同一 UA，curl(系统) 200、curl_cffi(chrome) 200，而 Python urllib 一律
   RemoteDisconnected（加 Accept / Accept-Language 无效）。
   所以通道默认引擎是 `curl_cffi`（进程内、39 ms），退化顺序见 `_once()`。

这里用同步实现 + 线程池，M0 阶段够用；
M3 接入 WebSocket 时整体换成 httpx.AsyncClient 即可，接口签名保持不变。
"""
from __future__ import annotations

import logging
import random
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("board.channel")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 只给 UA。实测：带上 Referer 会被东财 WAF 直接断连。
BASE_HEADERS = {"User-Agent": UA}

try:                                   # 首选引擎：进程内浏览器指纹
    from curl_cffi import requests as _cffi
except ImportError:                    # 没装就退回 urllib
    _cffi = None                       # type: ignore

_CURL = shutil.which("curl")
_TLS = threading.local()               # 每线程一个 Session，避免跨线程复用连接
_CA_WARNED = False


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
    engine: "auto"（默认，curl_cffi 优先）/ "urllib" / "curl"
    """

    def __init__(self, hosts: list[str] | tuple[str, ...], qps: float = 10,
                 timeout: float = 6, tries: int = 3,
                 breaker_fails: int = 5, breaker_sec: float = 60,
                 engine: str = "auto"):
        self.hosts = list(hosts)
        self.timeout = timeout
        self.tries = tries
        self.engine = engine
        self.bucket = TokenBucket(qps)
        self.breaker = CircuitBreaker(breaker_fails, breaker_sec)
        self.requests = 0
        self.success = 0
        self.host_hits: dict[str, int] = {}
        self._rr = 0

    def _via_curl(self, url: str) -> bytes:
        """用系统 curl 发请求。返回码非 0 或空响应都算失败。"""
        if not _CURL:
            raise RuntimeError("系统无 curl")
        cp = subprocess.run(
            [_CURL, "-sS", "--compressed", "-A", UA,
             "-H", "Accept: */*", "-m", str(self.timeout), url],
            capture_output=True, timeout=self.timeout + 3)
        if cp.returncode != 0:
            raise RuntimeError(f"curl rc={cp.returncode} {cp.stderr[:120]!r}")
        if not cp.stdout:
            raise RuntimeError("curl 空响应")
        return cp.stdout

    def _via_cffi(self, url: str) -> bytes:
        """curl_cffi：进程内伪造 Chrome 的 TLS/JA3 指纹。

        本机 `.venv` 的路径含中文，libcurl 读 certifi 的 CA 文件会报 (77)
        "error adding trust anchors" —— 这是路径编码问题，不是证书问题。
        所以先按正常校验发，只在 SSL 错误时退到 verify=False 并提示一次。
        """
        if _cffi is None:
            raise RuntimeError("curl_cffi 未安装")
        sess = getattr(_TLS, "sess", None)
        if sess is None:
            sess = _cffi.Session(impersonate="chrome")
            _TLS.sess = sess
        try:
            r = sess.get(url, timeout=self.timeout)
        except Exception as e:                    # noqa: BLE001
            if "curl: (77)" not in str(e) and "SSL" not in str(e):
                raise
            global _CA_WARNED
            if not _CA_WARNED:
                _CA_WARNED = True
                log.warning("CA 文件读取失败（中文路径），本进程改用不校验模式：%s",
                            str(e)[:120])
            r = sess.get(url, timeout=self.timeout, verify=False)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        if not r.content:
            raise RuntimeError("空响应")
        return r.content

    def _once(self, url: str) -> bytes:
        """引擎分派。

        `engine="cffi"` 是**严格单引擎**：已知某条路径被封时用它，失败要快。
        否则 urllib(6s) → cffi(6s) → curl(9s) 一轮就 20 s，多页接口会拖到几分钟，
        上层永远等不到兜底分支。
        """
        if self.engine == "cffi":
            return self._via_cffi(url)
        order = (("urllib", "cffi", "curl") if self.engine == "urllib"
                 else ("curl", "cffi", "urllib") if self.engine == "curl"
                 else ("cffi", "urllib", "curl"))
        last: Exception | None = None
        for eng in order:
            try:
                if eng == "cffi":
                    return self._via_cffi(url)
                if eng == "curl":
                    return self._via_curl(url)
                req = urllib.request.Request(url, headers=BASE_HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read()
            except Exception as e:                # noqa: BLE001
                last = e
        raise RuntimeError(f"三种引擎均失败: {last!r}")

    def fetch(self, path: str, encoding: str = "utf-8") -> bytes:
        """按域名轮换 + 退避重试拉取；全失败抛 ChannelError。"""
        if self.breaker.is_open:
            raise CircuitOpen(f"熔断中，剩余 {self.breaker.remain:.0f}s")

        last: Exception | None = None
        for i in range(self.tries):
            # 起始域名轮转：如果每次都从 push2 开始，重试压力全压在一个域名上
            self._rr += 1
            host = self.hosts[(self._rr + i) % len(self.hosts)]
            self.bucket.take()
            self.requests += 1
            url = f"https://{host}{path}" if path.startswith("/") else path
            try:
                raw = self._once(url)
                self.success += 1
                self.breaker.success()
                self.host_hits[host] = self.host_hits.get(host, 0) + 1
                return raw
            except Exception as e:            # noqa: BLE001 — 网络异常一律重试
                last = e
                self.breaker.failure()
                # 指数退避 + 抖动，避免多线程同步重试形成尖峰
                time.sleep(0.4 * (2 ** i) + random.uniform(0, 0.25))

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
