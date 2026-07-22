#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gitee 仓库选择器
================
按清单要求从候选清单中筛选 30–40 个主仓：
  - 主仓在 Gitee（非 GitHub 镜像）
  - AI/LLM 类与通用开源类大致各半
  - 活跃度门槛（近 2 年 issue/PR 量级可比拟 GitHub 对照仓）
  - 覆盖不同治理主体（企业主导 / 基金会托管 / 社区自发）

输出：
  - modelscope_output/gitee_selected_repos.json
  - modelscope_output/gitee_repo_candidates.json（含筛选过程元数据）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ============================================================================
# 配置
# ============================================================================
try:
    from _secrets import GITEE_TOKEN, GITEE_TOKENS
except ImportError:
    GITEE_TOKEN = os.environ.get("GITEE_TOKEN", "")
    GITEE_TOKENS = [t for t in os.environ.get("GITEE_TOKENS", "").split(",") if t]

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "modelscope_output"
OUTPUT_DIR.mkdir(exist_ok=True)

OUT_SELECTED = OUTPUT_DIR / "gitee_selected_repos.json"
OUT_CANDIDATES = OUTPUT_DIR / "gitee_repo_candidates.json"

GITEE_API = "https://gitee.com/api/v5"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

REQUEST_DELAY = 0.5
MAX_PER_PAGE = 100


class TokenRotator:
    """Gitee token 轮换器，支持多 token 避免限流。"""
    def __init__(self):
        self.tokens = list(dict.fromkeys(GITEE_TOKENS or ([GITEE_TOKEN] if GITEE_TOKEN else [])))
        self.idx = 0
        self.failed = set()

    def current(self) -> str | None:
        available = [t for t in self.tokens if t not in self.failed]
        if not available:
            return None
        return available[self.idx % len(available)]

    def next(self):
        available = [t for t in self.tokens if t not in self.failed]
        if available:
            self.idx = (self.idx + 1) % len(available)

    def mark_failed(self, token: str):
        self.failed.add(token)


TOKEN_ROTATOR = TokenRotator()


def fetch_paginated(session: requests.Session, path: str, params: dict = None,
                    since: str = None, since_key: str = "since") -> list:
    """分页拉取 Gitee 列表接口。"""
    all_items = []
    p = dict(params) if params else {}
    p["per_page"] = MAX_PER_PAGE
    page = 1
    consecutive_empty = 0

    while True:
        p["page"] = page
        if since and since_key:
            p[since_key] = since

        data, status = gitee_get(session, path, p)
        if status != 200:
            break
        if not isinstance(data, list):
            break
        if not data:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
        else:
            consecutive_empty = 0
            all_items.extend(data)
            # 如果最后一项已经早于 since，可以提前终止
            if since and data:
                last_created = data[-1].get("created_at") or data[-1].get("createdAt", "")
                if last_created and last_created < since:
                    break

        if len(data) < MAX_PER_PAGE:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return all_items


# 候选仓库：AI/LLM 类 + 通用对照类
CANDIDATES = {
    # AI/LLM 类
    "AI_LLM": [
        ("mindspore", "mindspore"),
        ("mindspore", "mindspore-lite"),
        ("mindspore", "mindspore-ascend"),
        ("Jittor", "Jittor"),
        ("Jittor", "jittorllms"),
        ("opengauss", "openGauss"),
        ("opengauss", "openGauss-AI"),
        ("OpenAtomFoundation", "prompt-java"),
        ("OpenAtomFoundation", "pacific-ai"),
        ("InternLM", "InternLM"),
        ("baichuan-inc", "Baichuan2"),
        ("ZhipuAI", "ChatGLM-6B"),
        ("ZhipuAI", "chatglm3-6b"),
        ("XiaoHuAI", "AgentVerse"),
        ("OpenBMB", "CPM-Bee"),
    ],
    # 通用对照类
    "GENERAL": [
        ("openeuler", "openEuler"),
        ("openharmony", "openharmony"),
        ("opengauss", "openGauss"),
        ("openlookeng", "openLooKeng"),
        ("apache", "dubbo"),
        ("apache", "rocketmq"),
        ("apache", "shardingsphere"),
        ("sofastack", "sofa-boot"),
        ("alibaba", "nacos"),
        ("alibaba", "sentinel"),
        ("alibaba", "arthas"),
        ("Tencent", "Matrix"),
        ("Tencent", "wcdb"),
        ("Tencent", "Tendis"),
        ("Huawei", "DevEco-Device-Tool"),
        ("pingcap", "tidb"),
        ("vuejs", "vue"),
        ("labuladong", "fucking-algorithm"),
    ],
}

# 活跃度门槛：近 2 年 issue + PR >= 50
ACTIVITY_THRESHOLD = 50


# ============================================================================
# 工具函数
# ============================================================================
def gitee_get(session: requests.Session, path: str, params: dict = None) -> tuple:
    """请求 Gitee API，返回 (data, status)；支持 token 轮换。"""
    url = f"{GITEE_API}{path}"
    p = dict(params) if params else {}

    for attempt in range(10):
        token = TOKEN_ROTATOR.current()
        if not token:
            return None, 401
        p["access_token"] = token

        try:
            r = session.get(url, headers=HEADERS, params=p, timeout=30)
            if r.status_code == 200:
                try:
                    return r.json(), 200
                except Exception:
                    return r.text, 200
            if r.status_code == 404:
                return None, 404
            if r.status_code in (429, 403):
                # 可能当前 token 限流，尝试下一个
                TOKEN_ROTATOR.mark_failed(token)
                TOKEN_ROTATOR.next()
                time.sleep(1)
                continue
            return None, r.status_code
        except Exception as e:
            if attempt < 9:
                time.sleep(min(2 ** (attempt % 4), 10))
                continue
            return str(e), -1
    return None, -1


def count_since(items: list, since: str) -> int:
    """统计 since 时间之后创建的项目数（假设字段为 created_at）。"""
    cnt = 0
    for item in items:
        created = item.get("created_at") or item.get("createdAt") or ""
        if created and created >= since:
            cnt += 1
    return cnt


def classify_governance(owner_type: str, description: str, org_login: str) -> str:
    """简单判断治理主体类型。"""
    desc = (description or "").lower()
    if owner_type == "Organization":
        if any(k in org_login.lower() for k in ["foundation", "apache", "openatom", "openeuler", "openharmony", "opengauss"]):
            return "foundation"
        return "enterprise"
    return "community"


# ============================================================================
# 主流程
# ============================================================================
def evaluate_repo(session: requests.Session, owner: str, repo: str, since: str) -> dict:
    """评估单个候选仓库。"""
    result = {
        "owner": owner,
        "repo": repo,
        "full_name": f"{owner}/{repo}",
        "status": "pending",
        "is_mirror": None,
        "activity_2y": 0,
        "governance_type": None,
        "selected": False,
        "reason": "",
    }

    repo_info, status = gitee_get(session, f"/repos/{owner}/{repo}")
    if status != 200 or not isinstance(repo_info, dict):
        result["status"] = "error"
        result["reason"] = f"API error {status}"
        return result

    result["status"] = "ok"
    result["repo_info"] = {
        "id": repo_info.get("id"),
        "name": repo_info.get("name"),
        "full_name": repo_info.get("full_name"),
        "description": repo_info.get("description"),
        "stargazers_count": repo_info.get("stargazers_count"),
        "watchers_count": repo_info.get("watchers_count"),
        "forks_count": repo_info.get("forks_count"),
        "open_issues_count": repo_info.get("open_issues_count"),
        "created_at": repo_info.get("created_at"),
        "updated_at": repo_info.get("updated_at"),
        "owner_type": (repo_info.get("owner") or {}).get("type"),
        "owner_login": (repo_info.get("owner") or {}).get("login"),
    }

    # 检查镜像关系
    # Gitee API 中 parent / source / mirror_from 字段可能暴露镜像来源
    mirror_url = repo_info.get("mirror_url") or repo_info.get("html_url") or ""
    parent = repo_info.get("parent") or repo_info.get("source")
    has_github_parent = bool(parent and "github.com" in json.dumps(parent))
    result["is_mirror"] = has_github_parent or "github.com" in mirror_url
    if result["is_mirror"]:
        result["reason"] = "GitHub mirror"
        return result

    # 治理主体
    result["governance_type"] = classify_governance(
        result["repo_info"]["owner_type"],
        result["repo_info"]["description"],
        result["repo_info"]["owner_login"],
    )

    # 活跃度：近 since 以来 issues + PRs（分页获取）
    issues = fetch_paginated(session, f"/repos/{owner}/{repo}/issues",
                             {"state": "all", "sort": "created", "direction": "desc"},
                             since=since)
    pulls = fetch_paginated(session, f"/repos/{owner}/{repo}/pulls",
                            {"state": "all", "sort": "created", "direction": "desc"},
                            since=since)

    result["activity_2y"] = len(issues) + len(pulls)

    if result["activity_2y"] < ACTIVITY_THRESHOLD:
        result["reason"] = f"activity too low ({result['activity_2y']})"
        return result

    result["selected"] = True
    result["reason"] = "selected"
    return result


def main():
    parser = argparse.ArgumentParser(description="Gitee 仓库选择器")
    parser.add_argument("--since", default="2024-01-01", help="活跃度统计起始日期（默认 2024-01-01）")
    parser.add_argument("--target", type=int, default=35, help="目标仓库数（默认 35）")
    parser.add_argument("--dry-run", action="store_true", help="只输出候选，不写入文件")
    args = parser.parse_args()

    if not GITEE_TOKEN:
        print("[warn] 未设置 GITEE_TOKEN，可能触发严格限速。")

    session = requests.Session()
    session.headers.update(HEADERS)

    all_candidates = []
    for category, repos in CANDIDATES.items():
        for owner, repo in repos:
            print(f"[eval] {owner}/{repo} ...")
            info = evaluate_repo(session, owner, repo, args.since)
            info["category"] = category
            all_candidates.append(info)
            time.sleep(REQUEST_DELAY)

    # 按类别分别选取，保持 AI/LLM 与通用大致各半
    selected = []
    ai_selected = [c for c in all_candidates if c["category"] == "AI_LLM" and c["selected"]]
    gen_selected = [c for c in all_candidates if c["category"] == "GENERAL" and c["selected"]]

    half = args.target // 2
    selected.extend(ai_selected[:half])
    selected.extend(gen_selected[:half])

    # 若某一类不足，用另一类补
    if len(selected) < args.target:
        remaining = [c for c in all_candidates if c["selected"] and c not in selected]
        remaining.sort(key=lambda x: -x["activity_2y"])
        selected.extend(remaining[:args.target - len(selected)])

    selected = selected[:args.target]

    print(f"\n[summary] 候选总数: {len(all_candidates)}, 选中: {len(selected)}")
    print(f"  AI/LLM: {len([s for s in selected if s['category']=='AI_LLM'])}")
    print(f"  General: {len([s for s in selected if s['category']=='GENERAL'])}")
    print(f"  Governance: enterprise={len([s for s in selected if s['governance_type']=='enterprise'])}, "
          f"foundation={len([s for s in selected if s['governance_type']=='foundation'])}, "
          f"community={len([s for s in selected if s['governance_type']=='community'])}")

    if args.dry_run:
        print("\n[detailed candidates]")
        for c in all_candidates:
            print(f"  {c['full_name']:40s} selected={str(c['selected']):5s} status={c['status']:6s} "
                  f"mirror={c['is_mirror']} activity={c['activity_2y']:4d} gov={c['governance_type'] or 'N/A':12s} reason={c['reason']}")
        print("\n[selected]")
        for s in selected:
            print(f"  {s['full_name']}  activity_2y={s['activity_2y']}  gov={s['governance_type']}")
        return

    with open(OUT_SELECTED, "w", encoding="utf-8") as f:
        json.dump(selected, f, ensure_ascii=False, indent=2)

    with open(OUT_CANDIDATES, "w", encoding="utf-8") as f:
        json.dump(all_candidates, f, ensure_ascii=False, indent=2)

    print(f"[save] {OUT_SELECTED}")
    print(f"[save] {OUT_CANDIDATES}")


if __name__ == "__main__":
    main()
