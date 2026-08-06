#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进度监控提交脚本 - 爬虫运行期间每 N 秒把最新进度提交到 GitHub。

用法: python watch_commit.py <爬虫进程PID> <间隔秒>
- 轮询爬虫进程是否存活；存活期间每 <间隔> 秒读取状态文件并 commit+push 进度。
- 爬虫进程结束后退出。

环境变量 GITHUB_TOKEN 由 GitHub Actions 自动提供（需 contents: write 权限）。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "modelscope_output")

SITE_TOTALS = {
    "models": 226815,
    "datasets": 38356,
    "skills": 76173,
    "studios": 13300,
    "mcp": 9781,
}


def log(msg):
    print(f"[watch] {msg}", flush=True)


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_progress():
    """生成各进度文件（与 workflow 末尾逻辑一致）"""
    now = datetime.now(timezone.utc).isoformat()

    # progress_models.json
    try:
        state = load_json(os.path.join(OUT_DIR, "state_ms_models_full.json")) or []
        models = load_json(os.path.join(OUT_DIR, "models_full.json"))
        prog = {
            "models": len(models) if models else 0,
            "termsCompleted": len(state),
            "siteTotal": SITE_TOTALS["models"],
            "updatedAt": now,
        }
        with open(os.path.join(BASE_DIR, "progress_models.json"), "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"progress_models 生成失败: {e}")

    # progress_lists.json（datasets/skills/studios/mcp）
    try:
        prog = {"updatedAt": now, "siteTotals": SITE_TOTALS, "sections": {}}
        for kind in ("datasets", "skills", "studios", "mcp"):
            terms = 0
            st = load_json(os.path.join(OUT_DIR, f"state_ms_{kind}_full.json"))
            if st:
                terms = len(st)
            items = load_json(os.path.join(OUT_DIR, f"{kind}_full.json"))
            prog["sections"][kind] = {
                "collected": len(items) if items else 0,
                "termsCompleted": terms,
            }
        with open(os.path.join(BASE_DIR, "progress_lists.json"), "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"progress_lists 生成失败: {e}")


def run_git(cmd):
    """执行 git 命令，返回 (returncode, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def git_commit_push():
    """提交进度文件并推送。失败不影响主流程。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log("无 GITHUB_TOKEN，跳过提交")
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    remote = f"https://x-access-token:{token}@github.com/{repo}.git"
    ts = datetime.now(timezone.utc).strftime("%F %H:%M")

    rc, so, se = run_git(["git", "config", "user.name", "github-actions[bot]"])
    run_git(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run_git(["git", "add", "progress.json", "progress_models.json", "progress_lists.json",
             "progress_sections.json", "progress_texts.json"])

    # 有变更才 commit
    rc, so, se = run_git(["git", "diff", "--cached", "--quiet"])
    if rc == 0:
        log("无进度变更，跳过提交")
        return
    rc, so, se = run_git(["git", "commit", "-m", f"data: 爬取实时进度 {ts}"])
    if rc != 0:
        log(f"commit 失败: {se[:200]}")
        return

    # push（先 pull --rebase 避免冲突，最多重试 3 次）
    for attempt in range(3):
        run_git(["git", "pull", "--rebase", remote, "main"])
        rc, so, se = run_git(["git", "push", remote, "main"])
        if rc == 0:
            log(f"进度已提交 {ts}")
            return
        log(f"push 失败(第{attempt+1}次): {se[:150]}")
        time.sleep(10)
    log("push 3 次失败，留给下轮重试")


def process_alive(pid):
    """检查进程是否存活（Linux 优先用 /proc，兼容 Windows）"""
    try:
        if os.name == "posix":
            return os.path.exists(f"/proc/{pid}")
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False
    except Exception:
        return True


def main():
    if len(sys.argv) < 2:
        log("用法: python watch_commit.py <爬虫PID> [间隔秒]")
        sys.exit(2)
    crawler_pid = int(sys.argv[1])
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 1800  # 默认 30 分钟
    log(f"启动监控: 爬虫PID={crawler_pid} 间隔={interval}s")

    # 先提交一次当前进度
    build_progress()
    git_commit_push()

    while True:
        time.sleep(interval)
        if not process_alive(crawler_pid):
            log("爬虫进程已结束，退出监控")
            break
        build_progress()
        git_commit_push()

    log("监控结束")


if __name__ == "__main__":
    main()
