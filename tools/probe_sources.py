# -*- coding: utf-8 -*-
"""分析看盘 · 数据源可行性探测（一次性脚本，结果写 docs/probe-result.json）

回答四个问题：
  1. 免费源能否支撑「秒级」全市场刷新？全市场一轮真实耗时多少？
  2. 五档盘口 / 内外盘 / 逐笔成交 能拿到哪些字段？
  3. 板块三维（行业/概念/地域）与资金流排行、龙虎榜是否可用？
  4. 东方财富的反爬现在是什么口径？（Referer / 重试 / pz 上限）

用法：
    NO_PROXY='*' python tools/probe_sources.py
    NO_PROXY='*' python tools/probe_sources.py --codes E:/path/codes.json

不做任何写库动作，纯只读探测。
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("NO_PROXY", "*")
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)
sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "probe-result.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
H = {"User-Agent": UA}          # 关键：不带 Referer（实测带上必被 WAF 断连）
TX = "https://qt.gtimg.cn/q="
EM_HOSTS = ["push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com"]
R = {}


def get(url, enc="utf-8", timeout=8):
    req = urllib.request.Request(url, headers=H)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode(enc, "replace"), round((time.perf_counter() - t0) * 1000, 1), len(raw)


def em(path, tries=3):
    """东财必须重试：裸请求实测 0/15 成功。每次重试换域名 + 指数退避。"""
    last = None
    for i in range(tries):
        host = EM_HOSTS[i % len(EM_HOSTS)]
        try:
            txt, ms, n = get(f"https://{host}{path}")
            return json.loads(txt), ms, n, host
        except Exception as e:
            last = repr(e)[:120]
            time.sleep(0.25 * (2 ** i) + random.uniform(0, 0.15))
    raise RuntimeError(last)


def sym_of(code):
    """代码 → 腾讯符号。北交所（含 920xxx）必须走 bj，否则静默返回空。"""
    c = str(code).zfill(6)
    if c[:2] == "92" or c[0] in "48" or c[:2] in ("43", "83", "87", "88"):
        return "bj" + c
    if c[0] in "69":
        return "sh" + c
    return "sz" + c


def tx_snap(syms):
    txt, ms, n = get(TX + ",".join(syms), enc="gbk")
    out = {}
    for line in txt.split(";"):
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].replace("v_", "").strip()
        out[key] = line.split('"')[1].split("~")
    return out, ms, n


# ── 代码清单：优先用外部 codes.json，否则用内置样本 ──────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--codes", default=r"E:\workbuddy_work\测试\.build\codes.json")
args = ap.parse_args()
if os.path.exists(args.codes):
    codes = json.load(open(args.codes, encoding="utf-8"))
    R["codes_source"] = args.codes
else:
    codes = [f"{i:06d}" for i in range(1, 601)]
    R["codes_source"] = "builtin-sample(600)"
all_syms = [sym_of(c) for c in codes]

# ── 1. 腾讯快照：单批延迟 + 全市场一轮 ────────────────────────────
d, ms1, n1 = tx_snap(all_syms[:300])
R["tx_snapshot"] = {"batch": 300, "hit": len(d), "ms_cold": ms1, "bytes": n1}

warm = []
for _ in range(8):
    _, msx, _ = tx_snap(all_syms[:300])
    warm.append(msx)
R["tx_snapshot"]["ms_warm_8"] = warm
R["tx_snapshot"]["ms_warm_avg"] = round(sum(warm) / len(warm), 1)

batches = [all_syms[i:i + 300] for i in range(0, len(all_syms), 300)]
t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=6) as ex:
    hit = sum(len(x[0]) for x in ex.map(tx_snap, batches))
wall = (time.perf_counter() - t0) * 1000
R["tx_full_market"] = {"codes": len(all_syms), "batches": len(batches), "workers": 6,
                       "wall_ms": round(wall, 1), "hit": hit,
                       "hit_rate": round(hit / len(all_syms), 4)}

# ── 2. 指数（含北证50 字段一致性复核）─────────────────────────────
idx = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000905", "sh000688", "bj899050"]
di, msi, _ = tx_snap(idx)
R["index"] = {"ms": msi, "rows": {}}
for k, v in di.items():
    if len(v) < 38:
        continue
    try:
        chk = round(float(v[3]) - float(v[4]) - float(v[31] or 0), 4)
    except (ValueError, IndexError):
        chk = None
    R["index"]["rows"][k] = {"name": v[1], "price": v[3], "prev": v[4], "pct": v[32],
                             "amount_wan": v[37], "n_fields": len(v), "check_p3_p4_p31": chk}

# ── 3. 延迟：服务端时间戳 vs 本机 ────────────────────────────────
_, _, _ = tx_snap(["sh000001"])
dl = 0.0
for line in tx_snap(["sh000001"])[0].values():
    pass
txt = get(TX + "sh000001", enc="gbk")[0]
stamp = txt.split('"')[1].split("~")[30]
try:
    st = time.strptime(stamp, "%Y%m%d%H%M%S")
    dl = time.time() - time.mktime(st)
except Exception:
    pass
R["latency"] = {"server_ts": stamp, "local": time.strftime("%Y%m%d%H%M%S"),
                "lag_sec": round(dl, 1)}

# ── 4. 字段解剖（沪市个股 + 北证50）───────────────────────────────
txt = get(TX + "sh600519,bj899050", enc="gbk")[0]
R["field_dict"] = {}
for line in txt.split(";"):
    if '"' not in line:
        continue
    key = line.split("=")[0].replace("v_", "").strip()
    f = line.split('"')[1].split("~")
    R["field_dict"][key] = {"n_fields": len(f), "fields": {str(i): v for i, v in enumerate(f)}}

# ── 5. 东财：裸请求 vs 重试成功率 ────────────────────────────────
PATH_BOARD = ("/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
              "&fs=m:90+t:2+f:!50&fields=f12,f14,f3,f6,f62,f104,f105,f128")
raw_ok = 0
for _ in range(12):
    try:
        get("https://push2.eastmoney.com" + PATH_BOARD)
        raw_ok += 1
    except Exception:
        pass
retry_ok = 0
for _ in range(12):
    try:
        em(PATH_BOARD)
        retry_ok += 1
    except Exception:
        pass
    time.sleep(0.1)
R["em_reliability"] = {"raw_success": f"{raw_ok}/12", "retry_success": f"{retry_ok}/12",
                       "raw_rate": round(raw_ok / 12, 3), "retry_rate": round(retry_ok / 12, 3)}

# Referer 对照（验证反爬口径）
try:
    req = urllib.request.Request("https://push2.eastmoney.com" + PATH_BOARD,
                                 headers=dict(H, Referer="https://quote.eastmoney.com/"))
    urllib.request.urlopen(req, timeout=6).read()
    R["em_reliability"]["with_referer"] = "OK"
except Exception as e:
    R["em_reliability"]["with_referer"] = f"FAIL {type(e).__name__}"

# ── 6. 板块三维 + pz 上限 + 分页成本 ───────────────────────────
R["em_board"] = {}
for name, fs in [("industry", "m:90+t:2+f:!50"), ("concept", "m:90+t:3+f:!50"),
                 ("region", "m:90+t:1+f:!50")]:
    try:
        j, ms, n, host = em(f"/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                            f"&fs={fs}&fields=f12,f14,f3,f6,f62,f104,f105,f128")
        dd = (j.get("data") or {}).get("diff") or []
        R["em_board"][name] = {"total": (j.get("data") or {}).get("total"), "got": len(dd),
                               "ms": ms, "host": host,
                               "top": [(x.get("f14"), x.get("f3"), x.get("f62")) for x in dd[:3]]}
    except Exception as e:
        R["em_board"][name] = {"err": repr(e)[:120]}

pz_probe = {}
for pz in (100, 500, 2000):
    try:
        j, _, _, _ = em(f"/api/qt/clist/get?pn=1&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f3"
                        "&fs=m:90+t:2+f:!50&fields=f12,f14,f3")
        pz_probe[pz] = len((j.get("data") or {}).get("diff") or [])
    except Exception as e:
        pz_probe[pz] = repr(e)[:60]
R["em_pz_limit"] = pz_probe

t0 = time.perf_counter()
pages, rows = 0, 0
for pn in range(1, 6):
    try:
        j, _, _, _ = em(f"/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                        "&fs=m:90+t:2+f:!50&fields=f12,f14,f3,f62")
        k = len((j.get("data") or {}).get("diff") or [])
        rows += k
        pages += 1 if k else 0
    except Exception:
        pass
R["em_paging_cost"] = {"pages": 5, "ok_pages": pages, "rows": rows,
                       "sec": round(time.perf_counter() - t0, 2)}

# ── 7. 资金流：板块排行 / 个股排行 / 大盘分钟 ─────────────────
FLOW_F = "f12,f14,f2,f3,f62,f184,f66,f72,f78,f84"
R["em_flow"] = {}
for name, fs in [("industry", "m:90+t:2+f:!50"), ("concept", "m:90+t:3+f:!50"),
                 ("stock", "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23")]:
    try:
        j, ms, _, _ = em(f"/api/qt/clist/get?pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f62"
                         f"&fs={fs}&fields={FLOW_F}")
        dd = (j.get("data") or {}).get("diff") or []
        R["em_flow"][name] = {"total": (j.get("data") or {}).get("total"), "ms": ms,
                              "top": [(x.get("f14"), x.get("f62")) for x in dd[:3]]}
    except Exception as e:
        R["em_flow"][name] = {"err": repr(e)[:120]}

try:
    j, ms, _, _ = em("/api/qt/stock/fflow/kline/get?lmt=0&klt=1&secid=1.000001"
                     "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55", tries=4)
    kl = (j.get("data") or {}).get("klines") or []
    R["em_index_fflow_1min"] = {"n": len(kl), "ms": ms, "last": kl[-1] if kl else None}
except Exception as e:
    R["em_index_fflow_1min"] = {"err": repr(e)[:120]}

# ── 8. 逐笔成交 + 板块成分股 ────────────────────────────────
try:
    j, ms, _, _ = em("/api/qt/stock/details/get?secid=1.600519&pos=-15&iscca=1"
                     "&fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55")
    dd = (j.get("data") or {}).get("details") or []
    R["em_ticks"] = {"n": len(dd), "ms": ms, "sample": dd[:6],
                     "format": "时间,价格,成交量,笔数,方向(1卖/2买)"}
except Exception as e:
    R["em_ticks"] = {"err": repr(e)[:120]}

bk = None
for x in (R["em_board"].get("industry", {}).get("top") or []):
    pass
try:
    j, ms, _, _ = em("/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f3"
                     "&fs=m:90+t:2+f:!50&fields=f12,f14")
    bk = ((j.get("data") or {}).get("diff") or [{}])[0].get("f12")
    j2, ms2, _, _ = em(f"/api/qt/clist/get?pn=1&pz=10&po=1&np=1&fltt=2&invt=2&fid=f3"
                       f"&fs=b:{bk}&fields=f12,f14,f2,f3")
    dd = (j2.get("data") or {}).get("diff") or []
    R["em_board_members"] = {"bk": bk, "total": (j2.get("data") or {}).get("total"),
                             "got": len(dd), "ms": ms2,
                             "top": [(x.get("f14"), x.get("f3")) for x in dd[:3]]}
except Exception as e:
    R["em_board_members"] = {"err": repr(e)[:120]}

# ── 9. 全A口径 + 龙虎榜 ─────────────────────────────────────
try:
    j, ms, _, _ = em("/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3"
                     "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f3")
    R["em_universe"] = {"total": (j.get("data") or {}).get("total"), "ms": ms}
except Exception as e:
    R["em_universe"] = {"err": repr(e)[:120]}

try:
    # 数据中心必须带 sortColumns + sortTypes，否则静默返回 count=null
    j, ms, _, _ = em("/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL"
                     "&pageNumber=1&pageSize=3&sortColumns=TRADE_DATE&sortTypes=-1", tries=2)
    res = j.get("result") or {}
    R["em_lhb"] = {"count": res.get("count"), "ms": ms,
                   "keys": list((res.get("data") or [{}])[0].keys())[:8]}
except Exception as e:
    R["em_lhb"] = {"err": repr(e)[:120]}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump(R, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# ── 摘要 ────────────────────────────────────────────────
print("=" * 74)
print(f"代码源            : {R['codes_source']}  ({len(codes)} 只)")
print(f"腾讯单批 300 只   : 冷 {R['tx_snapshot']['ms_cold']} ms / 热 {R['tx_snapshot']['ms_warm_avg']} ms / {n1} B")
print(f"腾讯全市场一轮    : {R['tx_full_market']['codes']} 只 / {R['tx_full_market']['batches']} 批 / "
      f"{R['tx_full_market']['wall_ms']} ms / 命中率 {R['tx_full_market']['hit_rate']:.1%}")
print(f"行情延迟(源端)    : {R['latency']['lag_sec']} s")
print("指数字段一致性    :", {k: v["check_p3_p4_p31"] for k, v in R["index"]["rows"].items()})
print(f"东财 裸请求/重试  : {R['em_reliability']['raw_success']} / {R['em_reliability']['retry_success']}"
      f"   带Referer: {R['em_reliability'].get('with_referer')}")
print("板块三维          :", {k: (v.get("total"), v.get("got")) for k, v in R["em_board"].items()})
print("pz 上限探测       :", R["em_pz_limit"])
print("行业 5 页分页     :", R["em_paging_cost"])
print("资金流            :", {k: v.get("total") for k, v in R["em_flow"].items()})
print("大盘分钟资金流    :", R["em_index_fflow_1min"].get("n"), "根")
print("逐笔              :", R["em_ticks"].get("n"), "笔", R["em_ticks"].get("sample", [])[:2])
print("板块成分股        :", R["em_board_members"])
print("全A口径           :", R["em_universe"])
print("龙虎榜            :", R["em_lhb"].get("count"))
print("=" * 74)
print("detail ->", OUT)
