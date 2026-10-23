# -*- coding: utf-8 -*-
"""补测被限流打断的三项：板块成分股 / 全A口径 / 龙虎榜。请求间隔 2.5 s。"""
import json, os, random, sys, time, urllib.request

os.environ["NO_PROXY"] = "*"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)
sys.stdout.reconfigure(encoding="utf-8")

H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")}
HOSTS = ["push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com"]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "probe-supplement.json")
res = {}


def em(path, tries=4, gap=2.5):
    last = None
    for i in range(tries):
        host = HOSTS[i % len(HOSTS)]
        try:
            time.sleep(gap)
            r = urllib.request.Request(f"https://{host}{path}", headers=H)
            with urllib.request.urlopen(r, timeout=10) as x:
                return json.loads(x.read().decode("utf-8", "replace")), host
        except Exception as e:
            last = repr(e)[:140]
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(last)


print("=" * 70)
# 1) 板块列表 → 取真实 BK 代码 → 板块成分股
try:
    j, h = em("/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fltt=2&invt=2&fid=f3"
              "&fs=m:90+t:2+f:!50&fields=f12,f14,f3")
    dd = (j.get("data") or {}).get("diff") or []
    bks = [(x["f12"], x["f14"]) for x in dd[:5]]
    print("  行业板块样本:", bks)
    bk, bname = bks[0]
    j2, h2 = em(f"/api/qt/clist/get?pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f3"
                f"&fs=b:{bk}&fields=f12,f14,f2,f3,f62")
    d2 = j2.get("data") or {}
    mem = d2.get("diff") or []
    res["board_members"] = {"bk": bk, "name": bname, "total": d2.get("total"),
                            "got": len(mem), "host": h2,
                            "top": [(x.get("f14"), x.get("f3"), x.get("f62")) for x in mem[:5]]}
    print(f"  板块成分股 {bk} {bname}: total={d2.get('total')} got={len(mem)} "
          f"top={res['board_members']['top'][:3]}")
except Exception as e:
    res["board_members"] = {"err": repr(e)[:140]}
    print("  板块成分股 FAIL", repr(e)[:120])

# 2) 全A口径（三种 fs 写法对比）
for tag, fs in [("core", "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"),
                ("with_bj", "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048")]:
    try:
        j, h = em(f"/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields=f3")
        res[f"universe_{tag}"] = {"total": (j.get("data") or {}).get("total"), "host": h}
        print(f"  全A {tag:8s} total={res[f'universe_{tag}']['total']}")
    except Exception as e:
        res[f"universe_{tag}"] = {"err": repr(e)[:140]}
        print(f"  全A {tag:8s} FAIL", repr(e)[:100])

# 3) 龙虎榜（带 / 不带 sortColumns 对照）
for tag, extra in [("with_sort", "&sortColumns=TRADE_DATE&sortTypes=-1"), ("no_sort", "")]:
    try:
        j, h = em("/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL"
                  f"&pageNumber=1&pageSize=2{extra}")
        r = j.get("result") or {}
        rows = r.get("data") or []
        res[f"lhb_{tag}"] = {"count": r.get("count"), "rows": len(rows), "host": h,
                             "keys": list(rows[0].keys())[:10] if rows else []}
        print(f"  龙虎榜 {tag:9s} count={r.get('count')} rows={len(rows)}")
    except Exception as e:
        res[f"lhb_{tag}"] = {"err": repr(e)[:140]}
        print(f"  龙虎榜 {tag:9s} FAIL", repr(e)[:100])

# 4) 涨跌停池正确 reportName 试探
for rep in ["RPT_STOCK_ZT_POOL", "RPT_LIMIT_UP_POOL", "RPT_ZT_POOL_MAIN"]:
    try:
        j, h = em(f"/api/data/v1/get?reportName={rep}&columns=ALL&pageNumber=1&pageSize=2"
                  "&sortColumns=TRADE_DATE&sortTypes=-1", tries=2)
        r = j.get("result") or {}
        res[f"pool_{rep}"] = {"count": r.get("count")}
        print(f"  {rep:22s} count={r.get('count')}")
    except Exception as e:
        res[f"pool_{rep}"] = {"err": repr(e)[:100]}
        print(f"  {rep:22s} FAIL", repr(e)[:80])

json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("=" * 70)
print("saved ->", OUT)
