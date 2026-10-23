# -*- coding: utf-8 -*-
"""通过 GitHub REST API 推送仓库内容（当 git push 被网络阻断时的备用通道）。

为什么需要它：本机 HTTP 代理对 github.com 返回 502，git 的 HTTPS 传输走不通；
但 api.github.com 可正常访问，因此改用 Git Data API 一次性提交。

用法：
    set GITHUB_TOKEN=ghp_xxx
    python tools/push_via_api.py

    # 或显式传参
    python tools/push_via_api.py --token ghp_xxx --repo Solitude-0/AA --branch master

Token 权限：classic token 需 repo 范围；fine-grained token 需
    Repository permissions -> Contents: Read and write。

注意：github.com 域名不通不影响本脚本；脚本只访问 api.github.com。
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "state", "logs", ".idea", ".vscode"}
SKIP_EXT = {".pyc", ".pyo", ".log"}


def api(method, path, token, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "analysis-board-pusher",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def collect_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            out.append((rel, full))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--repo", default="Solitude-0/AA")
    ap.add_argument("--branch", default="master")
    ap.add_argument("--message", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.token:
        print("ERROR: 缺少 token。设置环境变量 GITHUB_TOKEN 或用 --token 传入。")
        sys.exit(2)

    files = collect_files()
    print(f"仓库   : {a.repo}")
    print(f"分支   : {a.branch}")
    print(f"文件数 : {len(files)}")
    for rel, _ in files:
        print(f"   {rel}")

    if a.dry_run:
        print("\n[dry-run] 未发起任何写操作。")
        return

    # 0) 校验 token 与仓库权限
    st, me = api("GET", "/user", a.token)
    print(f"\n认证用户: {me.get('login')}")
    st, repo = api("GET", f"/repos/{a.repo}", a.token)
    perms = repo.get("permissions") or {}
    print(f"仓库权限: admin={perms.get('admin')} push={perms.get('push')} "
          f"default_branch={repo.get('default_branch')}")
    if not perms.get("push"):
        print("ERROR: 该 token 对此仓库没有 push 权限。")
        sys.exit(3)

    # 1) 取父提交（空仓库没有）
    parent = None
    try:
        st, ref = api("GET", f"/repos/{a.repo}/git/ref/heads/{a.branch}", a.token)
        parent = (ref.get("object") or {}).get("sha")
        print(f"已有分支 : {a.branch} @ {parent[:8]}")
    except RuntimeError as e:
        # 空仓库取 ref 返回 409 "Git Repository is empty."，不是 404 —
        # 这是 GitHub 的实际行为，两者都视为"分支尚不存在"。
        if "HTTP 404" in str(e) or "HTTP 409" in str(e) or "empty" in str(e).lower():
            print(f"分支 {a.branch} 尚不存在（空仓库），将创建。")
        else:
            raise

    # 2) 上传 blob
    def put_blob(item):
        rel, full = item
        with open(full, "rb") as f:
            raw = f.read()
        st, r = api("POST", f"/repos/{a.repo}/git/blobs", a.token,
                    {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"})
        return rel, r["sha"], len(raw)

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(put_blob, files))
    total = sum(x[2] for x in results)
    print(f"已上传 blob: {len(results)} 个 / {total/1024:.1f} KB")

    # 3) 建 tree
    tree_items = [{"path": rel, "mode": "100644", "type": "blob", "sha": sha}
                  for rel, sha, _ in results]
    payload = {"tree": tree_items}
    if parent:
        st, base = api("GET", f"/repos/{a.repo}/git/commits/{parent}", a.token)
        payload["base_tree"] = base.get("tree", {}).get("sha")
    st, tree = api("POST", f"/repos/{a.repo}/git/trees", a.token, payload)
    print(f"已建 tree : {tree['sha'][:8]}")

    # 4) 建 commit
    msg = a.message or "docs: 分析看盘完整建设方案 v1.0（API 通道推送）"
    st, commit = api("POST", f"/repos/{a.repo}/git/commits", a.token,
                     {"message": msg, "tree": tree["sha"], "parents": [parent] if parent else []})
    print(f"已建 commit: {commit['sha'][:8]}")

    # 5) 更新或创建 ref
    if parent:
        st, _ = api("PATCH", f"/repos/{a.repo}/git/refs/heads/{a.branch}", a.token,
                    {"sha": commit["sha"], "force": False})
        print(f"已更新分支 : {a.branch}")
    else:
        st, _ = api("POST", f"/repos/{a.repo}/git/refs", a.token,
                    {"ref": f"refs/heads/{a.branch}", "sha": commit["sha"]})
        print(f"已创建分支 : {a.branch}")

    print(f"\n完成 -> https://github.com/{a.repo}/tree/{a.branch}")


if __name__ == "__main__":
    main()
