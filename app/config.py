# -*- coding: utf-8 -*-
"""全局配置。

原则：只放"需要调的旋钮"，不放业务逻辑。
所有值都能用环境变量覆盖，便于不改代码做现场降级。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

# ── 服务 ──────────────────────────────────────────────
PORT = int(os.environ.get("BOARD_PORT", "8768"))          # 避开现有 webapp 的 8767
HOST = os.environ.get("BOARD_HOST", "127.0.0.1")          # 仅本机，不对外暴露
DB_URL = os.environ.get("BOARD_DB_URL", "postgresql://postgres:postgres@127.0.0.1:5432/realtime")

# ── 腾讯快照通道 ──────────────────────────────────────
TX_URL = "https://qt.gtimg.cn/q="
TX_BATCH = 300              # 实测 300 只/批最优：146 KB、热响应 ~53 ms
TX_WORKERS = 6              # 并发批数；再高容易被限流
TX_QPS = 15                 # 令牌桶上限
TX_TIMEOUT = 8

# ── 东财通道（板块 / 资金流）────────────────────────────
EM_HOSTS = ("push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com")
EM_DC_HOST = "datacenter-web.eastmoney.com"   # 注意：龙虎榜不在 push2 域名下
EM_QPS = 10
EM_TIMEOUT = 6
EM_TRIES = 3
EM_BREAKER_FAILS = 5        # 连续失败几次触发熔断
EM_BREAKER_SEC = 60         # 熔断时长

# ── 节拍（秒）────────────────────────────────────────
TICK_INDEX = 1.0            # 指数 + 自选池
TICK_FULL = 3.0             # 全市场
TICK_BOARD = 5.0            # 板块三维
TICK_FLOW = 5.0             # 资金流
TICK_TICK = 5.0             # 逐笔
PERSIST_EVERY = 60.0        # 分钟级落库

# ── 自选池 ───────────────────────────────────────────
WATCHLIST_FILE = STATE / "watchlist.txt"
WATCHLIST_MAX = 200         # 一批上限
TICK_MAX_CODES = 20         # 逐笔只服这么多只（东财单只一次请求）

# ── 代码清单 ─────────────────────────────────────────
_codes_env = os.environ.get("BOARD_CODES", r"E:\workbuddy_work\测试\.build\codes.json")
CODES_FILE = Path(_codes_env)

# ── 校验阈值 ─────────────────────────────────────────
PRICE_TOL = 0.02            # |price - prev - chg| 容差
PCT_MAX = 31.0              # 涨跌幅上限（北交所 30% + 余量）
BAD_RATE_WARN = 0.005       # 单轮字段校验失败率告警线 0.5%
