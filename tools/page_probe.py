# -*- coding: utf-8 -*-
"""页面探针：用 CDP 直接驱动本机 Chromium，抓渲染结果、控制台错误与截图。

为什么不用 agent-browser / playwright：本机这两个在沙箱里起不来，
而 Chromium 二进制是现成的。CDP 协议本身很简单，一个 WS 连接足够。

跑法：
    NO_PROXY='*' python tools/page_probe.py
    NO_PROXY='*' python tools/page_probe.py --url http://127.0.0.1:8768/ --shot logs/page.png
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1217\chrome-win64\chrome.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return shutil.which("chrome") or shutil.which("chromium")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "probe"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as r:
        return json.loads(r.read().decode())


async def probe(url: str, shot: str | None, settle: float = 7.0) -> int:
    import websockets

    chrome = find_chrome()
    if not chrome:
        print("[!] 找不到 Chromium/Chrome/Edge")
        return 2

    port = 9333
    prof = tempfile.mkdtemp(prefix="board-probe-")
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--no-first-run", "--disable-extensions", "--mute-audio",
         f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
         "--window-size=1600,1000", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        ws_url = None
        for _ in range(40):
            try:
                tabs = _get_json(f"http://127.0.0.1:{port}/json")
                for t in tabs:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:              # noqa: BLE001
                pass
            await asyncio.sleep(0.25)
        if not ws_url:
            print("[!] 浏览器调试端口未就绪")
            return 3

        errors: list[str] = []
        logs: list[str] = []
        _id = 0

        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            async def send(method: str, params: dict | None = None) -> None:
                nonlocal _id
                _id += 1
                await ws.send(json.dumps({"id": _id, "method": method,
                                          "params": params or {}}))

            for m in ("Runtime.enable", "Log.enable", "Page.enable", "Network.enable"):
                await send(m)

            t0 = time.time()
            pending_eval: dict[int, str] = {}
            while time.time() - t0 < settle:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                method = msg.get("method")
                if method == "Runtime.exceptionThrown":
                    d = msg["params"]["exceptionDetails"]
                    errors.append(d.get("text", "") + " " +
                                  str((d.get("exception") or {}).get("description", ""))[:300])
                elif method == "Runtime.consoleAPICalled":
                    p = msg["params"]
                    if p.get("type") in ("error", "warning"):
                        args = " ".join(str(a.get("value", a.get("description", "")))
                                        for a in p.get("args", []))
                        logs.append(f"{p['type']}: {args[:300]}")
                elif method == "Log.entryAdded":
                    e = msg["params"]["entry"]
                    if e.get("level") in ("error", "warning"):
                        logs.append(f"{e['level']}: {e.get('text', '')[:300]}")

            async def evaluate(expr: str) -> str:
                nonlocal _id
                _id += 1
                mid = _id
                await ws.send(json.dumps({
                    "id": mid, "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True,
                               "awaitPromise": True}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    m = json.loads(raw)
                    if m.get("id") == mid:
                        r = (m.get("result") or {}).get("result") or {}
                        return str(r.get("value"))
                    if m.get("method") == "Runtime.exceptionThrown":
                        d = m["params"]["exceptionDetails"]
                        errors.append(str(d.get("text", ""))[:200])

            checks = await evaluate("""(() => ({
                idx: document.querySelectorAll('.idx').length,
                kpi: document.querySelectorAll('.kpi').length,
                cells: document.querySelectorAll('.cell').length,
                rows: document.querySelectorAll('table.grid tbody tr').length,
                alerts: document.querySelectorAll('.arow').length,
                ws: (document.querySelector('.badges .badge:nth-child(3)')||{}).textContent,
                breadth: (document.querySelector('.kpi .v')||{}).textContent,
                sample: (document.querySelector('table.grid tbody tr')||{}).innerText,
                bodyLen: document.body.innerText.length
            }))()""")

            if shot:
                _id += 1
                mid = _id
                await ws.send(json.dumps({"id": mid, "method": "Page.captureScreenshot",
                                          "params": {"format": "png"}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    m = json.loads(raw)
                    if m.get("id") == mid:
                        data = ((m.get("result") or {}).get("data") or "")
                        if data:
                            os.makedirs(os.path.dirname(shot) or ".", exist_ok=True)
                            with open(shot, "wb") as f:
                                f.write(base64.b64decode(data))
                        break

        print("渲染检查:", checks)
        if logs:
            print("控制台:")
            for x in logs[:8]:
                print("  ", x)
        if errors:
            print("运行时异常:")
            for x in errors[:8]:
                print("  ", x)
        bad = errors or any(k in checks for k in ("None",))
        return 1 if bad else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8768/")
    ap.add_argument("--shot", default="logs/page.png")
    ap.add_argument("--settle", type=float, default=7.0)
    a = ap.parse_args()
    sys.exit(asyncio.run(probe(a.url, a.shot, a.settle)))
