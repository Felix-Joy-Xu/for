#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进度监控提交脚本 - 爬虫运行期间每 30 分钟把最新进度提交到 GitHub。

用法: python watch_commit.py <爬虫进程PID> <间隔秒数>
- 轮询爬虫进程是否存活；存活期间每 <间隔> 秒读取状态文件并 commit+push 进度。
- 爬虫进程结束后退出（此时主 workflow 步骤会再写一次最终进度）。

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


def count_jsonl(path):
    """统计 jsonl 行数（快速）"""
    try:
        n = 0
        with open(path, "r", encoding="utf-8") as f:
            for _ in f:
                n += 1
        return n
    except Exception:
        return 0


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
        print(f"[watch] progress_models 生成失败: {e}", flush=True)

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
        print(f"[watch] progress_lists 生成失败: {e}", flush=True)


def git_commit_push():
    """提交进度文件并推送。失败不影响主流程。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("[watch] 无 GITHUB_TOKEN，跳过提交", flush=True)
        return
    remote = f"https://x-access-token:{token}@github.com/{os.environ.get('GITHUB_REPOSITORY', '')}.git"
    ts = datetime.now(timezone.utc).strftime("%F %H:%M")
    cmds = [
        ["git", "config", "user.name", "github-actions[bot]"],
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        ["git", "add", "progress.json", "progress_models.json", "progress_lists.json",
         "progress_sections.json", "progress_texts.json"],
        ["git", "commit", "-m", f"data: 爬取实时进度 {ts}"],
        ["git", "pull", "--rebase", remote, "main"],
        ["git", "push", remote, "main"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and cmd[0] != "git" and cmd[1] != "commit":
            # commit 无变更时返回 1，可忽略
            pass
        if "error" in (r.stderr or "").lower():
            print(f"[watch] {cmd[1] if len(cmd)>1 else cmd} 提示: {r.stderr[:100]}", flush=True)
    print(f"[watch] 进度已提交 {ts}", flush=True)


def main():
    if len(sys.argv) < 2:
        print("用法: python watch_commit.py <爬虫PID> [间隔秒]", flush=True)
        sys.exit(2)
    crawler_pid = int(sys.argv[1])
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 1800  # 默认 30 分钟

    # 先提交一次当前进度
    build_progress()
    git_commit_push()

    while True:
        time.sleep(interval)
        # 检查爬虫进程是否存活
        try:
            os.kill(crawler_pid, 0)
        except ProcessLookupError:
            print("[watch] 爬虫进程已结束，退出监控", flush=True)
            break
        except PermissionError:
            pass
        build_progress()
        git_commit_push()


if __name__ == "__main__":
    main()
