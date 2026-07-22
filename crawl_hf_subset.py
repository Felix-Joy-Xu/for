#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HuggingFace 子集行为数据采集
============================
在 crawl_hf_full.py 已生成 hf_models_all.jsonl 后运行。
子集构成：
  1. 与魔搭匹配的模型对（P0）
  2. 下载量 Top N（P1）
  3. 分层随机样本（按 pipeline_tag / library_name 分层，P1）

采集内容：
  - commits:      GET /api/models/{id}/commits
  - discussions:  GET /api/models/{id}/discussions
  - README:       GET /{id}/raw/main/README.md
  - config.json:  GET /{id}/raw/main/config.json
  - file tree:    GET /api/models/{id}/tree/main

输出：
  - modelscope_output/hf_commit_history.jsonl
  - modelscope_output/hf_comments_all.jsonl
  - modelscope_output/hf_model_cards/{safe_id}_README.md
  - modelscope_output/hf_model_configs/{safe_id}_config.json
  - modelscope_output/hf_model_dependencies.jsonl（含 tree 与 file_size）

断点：modelscope_output/state_hf_subset.json
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ============================================================================
# 配置
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "modelscope_output"
OUTPUT_DIR.mkdir(exist_ok=True)

HF_ORIGIN = "https://hf-mirror.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

MODELS_FILE = OUTPUT_DIR / "hf_models_all.jsonl"

# 对照表可能来自 HF 对照采集（cross_platform_match.json）或 02 更新匹配表（cross_platform_match_full.json）
def match_file() -> Path | None:
    for name in ["cross_platform_match.json", "cross_platform_match_full.json"]:
        p = OUTPUT_DIR / name
        if p.exists():
            return p
    return None

MATCH_FILE = match_file()

# 分片支持：通过 --shard-index/--total-shards 多 job 并行
SHARD_INDEX = 0
TOTAL_SHARDS = 1

def shard_suffix() -> str:
    return "" if TOTAL_SHARDS <= 1 else f"_shard_{SHARD_INDEX}"

COMMIT_FILE = OUTPUT_DIR / f"hf_commit_history{shard_suffix()}.jsonl"
COMMENT_FILE = OUTPUT_DIR / f"hf_comments_all{shard_suffix()}.jsonl"
DEP_FILE = OUTPUT_DIR / f"hf_model_dependencies{shard_suffix()}.jsonl"
CARD_DIR = OUTPUT_DIR / "hf_model_cards"
CONFIG_DIR = OUTPUT_DIR / "hf_model_configs"
STATE_FILE = OUTPUT_DIR / f"state_hf_subset{shard_suffix()}.json"

CARD_DIR.mkdir(exist_ok=True)
CONFIG_DIR.mkdir(exist_ok=True)

MAX_WORKERS = 4
REQUEST_DELAY = 0.6
SAVE_EVERY = 100

# 默认子集大小
DEFAULT_TOTAL = 6000
DEFAULT_TOP_N = 2000
DEFAULT_STRATIFIED = 3000


# ============================================================================
# 工具函数
# ============================================================================
def safe_id(model_id: str) -> str:
    return model_id.replace("/", "_").replace("\\", "_")


def load_jsonl(path: Path) -> list:
    records = []
    if not path.exists():
        return records
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed": [], "count": 0}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def select_subset(total: int = DEFAULT_TOTAL, top_n: int = DEFAULT_TOP_N,
                  stratified_n: int = DEFAULT_STRATIFIED) -> list:
    """返回选中的 model_id 列表。"""
    if not MODELS_FILE.exists():
        raise FileNotFoundError(f"未找到 {MODELS_FILE}，请先运行 crawl_hf_full.py")

    all_models = load_jsonl(MODELS_FILE)
    if not all_models:
        raise ValueError(f"{MODELS_FILE} 为空")

    # 取 _raw 中的实际 HF 字段
    models = []
    for rec in all_models:
        raw = rec.get("_raw") or rec
        if raw.get("id"):
            models.append(raw)

    ids = {m["id"] for m in models}
    by_id = {m["id"]: m for m in models}

    selected = set()

    # 1. 与魔搭匹配的模型对
    if MATCH_FILE and MATCH_FILE.exists():
        with open(MATCH_FILE, "r", encoding="utf-8") as f:
            matches = json.load(f)
        for m in matches:
            hf_id = m.get("hf_id")
            if hf_id and hf_id in ids:
                selected.add(hf_id)
        print(f"[subset] 与魔搭匹配: {len(selected)} 个")

    # 2. 下载量 Top N
    sorted_by_dl = sorted(models, key=lambda x: x.get("downloads", 0), reverse=True)
    top_ids = {m["id"] for m in sorted_by_dl[:top_n]}
    selected.update(top_ids)
    print(f"[subset] 加入 Top {top_n} 后: {len(selected)} 个")

    # 3. 分层随机样本
    remaining = [m for m in models if m["id"] not in selected]
    # 按 (pipeline_tag, library_name) 分层
    strata = defaultdict(list)
    for m in remaining:
        key = (m.get("pipeline_tag") or "unknown",
               m.get("library_name") or extract_library_name(m.get("tags", [])) or "unknown")
        strata[key].append(m)

    # 按比例分配名额，至少每层 1 个
    total_strata = len(strata)
    per_layer = stratified_n // total_strata if total_strata else 0
    extra = stratified_n - per_layer * total_strata
    sampled = []
    for i, (key, items) in enumerate(sorted(strata.items(), key=lambda kv: -len(kv[1]))):
        quota = per_layer + (1 if i < extra else 0)
        if len(items) <= quota:
            sampled.extend(items)
        else:
            sampled.extend(random.sample(items, quota))

    sampled_ids = {m["id"] for m in sampled}
    selected.update(sampled_ids)
    print(f"[subset] 加入分层样本 {len(sampled_ids)} 后: {len(selected)} 个")

    # 如果总数仍不足，从剩余中按下载量补
    if len(selected) < total:
        still_remaining = [m for m in models if m["id"] not in selected]
        still_remaining.sort(key=lambda x: x.get("downloads", 0), reverse=True)
        need = total - len(selected)
        selected.update(m["id"] for m in still_remaining[:need])
        print(f"[subset] 补足下载量后: {len(selected)} 个")

    result = [by_id[mid] for mid in selected if mid in by_id]
    random.shuffle(result)
    return result


def extract_library_name(tags):
    if not tags:
        return ""
    for t in tags:
        if isinstance(t, str) and t.startswith("library:"):
            return t.split(":", 1)[1]
    return ""


# ============================================================================
# 采集函数
# ============================================================================
def fetch_text(session: requests.Session, url: str, max_retries: int = 3) -> tuple:
    """抓取文本，返回 (content, status)。"""
    for attempt in range(max_retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and len(r.text) > 10:
                return r.text, 200
            if r.status_code in (429, 503):
                time.sleep(min(2 ** attempt * 2, 30))
                continue
            return r.text, r.status_code
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return str(e), -1
    return "", -1


def fetch_json(session: requests.Session, url: str, max_retries: int = 3) -> tuple:
    """抓取 JSON，返回 (data, status)。"""
    for attempt in range(max_retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                try:
                    return r.json(), 200
                except Exception:
                    return r.text, 200
            if r.status_code in (429, 503):
                time.sleep(min(2 ** attempt * 2, 30))
                continue
            return None, r.status_code
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return str(e), -1
    return None, -1


def fetch_model_subset(session: requests.Session, model_id: str, save_files: bool = True) -> dict:
    """采集单个模型的子集数据。"""
    result = {
        "model_id": model_id,
        "crawled_at": time.time(),
        "commits": {"status": "pending"},
        "discussions": {"status": "pending"},
        "readme": {"status": "pending"},
        "config": {"status": "pending"},
        "tree": {"status": "pending"},
    }

    # 1. commits
    commits_url = f"{HF_ORIGIN}/api/models/{model_id}/commits"
    commits_data, commits_status = fetch_json(session, commits_url)
    result["commits"] = {
        "status": "success" if commits_status == 200 else "error",
        "http_status": commits_status,
        "url": commits_url,
        "data": commits_data if commits_status == 200 else None,
    }

    # 2. discussions
    disc_url = f"{HF_ORIGIN}/api/models/{model_id}/discussions"
    disc_data, disc_status = fetch_json(session, disc_url)
    result["discussions"] = {
        "status": "success" if disc_status == 200 else "error",
        "http_status": disc_status,
        "url": disc_url,
        "data": disc_data if disc_status == 200 else None,
    }

    # 3. README
    readme_text, readme_status = "", 0
    for branch in ["main", "master"]:
        url = f"{HF_ORIGIN}/{model_id}/raw/{branch}/README.md"
        text, status = fetch_text(session, url)
        if status == 200 and len(text) > 30:
            readme_text, readme_status = text, status
            break
    result["readme"] = {
        "status": "success" if readme_status == 200 else "error",
        "http_status": readme_status,
        "length": len(readme_text),
    }
    if save_files and readme_status == 200:
        with open(CARD_DIR / f"{safe_id(model_id)}_README.md", "w", encoding="utf-8") as f:
            f.write(readme_text)

    # 4. config.json
    config_text, config_status = "", 0
    for branch in ["main", "master"]:
        url = f"{HF_ORIGIN}/{model_id}/raw/{branch}/config.json"
        text, status = fetch_text(session, url)
        if status == 200 and len(text) > 10:
            config_text, config_status = text, status
            break
    result["config"] = {
        "status": "success" if config_status == 200 else "error",
        "http_status": config_status,
        "length": len(config_text),
    }
    if save_files and config_status == 200:
        try:
            cfg_json = json.loads(config_text)
            with open(CONFIG_DIR / f"{safe_id(model_id)}_config.json", "w", encoding="utf-8") as f:
                json.dump(cfg_json, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # 5. tree / file_size
    tree_url = f"{HF_ORIGIN}/api/models/{model_id}/tree/main"
    tree_data, tree_status = fetch_json(session, tree_url)
    if tree_status != 200:
        tree_url = f"{HF_ORIGIN}/api/models/{model_id}/tree/master"
        tree_data, tree_status = fetch_json(session, tree_url)

    file_size = 0
    file_count = 0
    if tree_status == 200 and isinstance(tree_data, list):
        for f in tree_data:
            if f.get("type") == "file" and f.get("size"):
                file_size += f["size"]
                file_count += 1

    result["tree"] = {
        "status": "success" if tree_status == 200 else "error",
        "http_status": tree_status,
        "url": tree_url,
        "file_count": file_count,
        "file_size": file_size,
    }

    return result


def write_dep_record(model_id: str, tree_result: dict):
    """写依赖/文件树记录。"""
    record = {
        "model_id": model_id,
        "crawled_at": time.time(),
        "http_status": tree_result.get("http_status"),
        "file_count": tree_result.get("file_count", 0),
        "file_size": tree_result.get("file_size", 0),
    }
    with open(DEP_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ============================================================================
# 主流程
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="HuggingFace 子集行为数据采集")
    parser.add_argument("--total", type=int, default=DEFAULT_TOTAL, help=f"子集总数（默认 {DEFAULT_TOTAL}）")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help=f"Top N 下载量（默认 {DEFAULT_TOP_N}）")
    parser.add_argument("--stratified-n", type=int, default=DEFAULT_STRATIFIED,
                        help=f"分层随机样本数（默认 {DEFAULT_STRATIFIED}）")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"并发数（默认 {MAX_WORKERS}）")
    parser.add_argument("--dry-run", action="store_true", help="只打印子集，不采集")
    parser.add_argument("--shard-index", type=int, default=0, help="当前分片索引（从 0 开始）")
    parser.add_argument("--total-shards", type=int, default=1, help="总分片数")
    args = parser.parse_args()

    global SHARD_INDEX, TOTAL_SHARDS
    SHARD_INDEX = args.shard_index
    TOTAL_SHARDS = args.total_shards

    # 重新计算带分片后缀的路径
    global COMMIT_FILE, COMMENT_FILE, DEP_FILE, STATE_FILE
    COMMIT_FILE = OUTPUT_DIR / f"hf_commit_history{shard_suffix()}.jsonl"
    COMMENT_FILE = OUTPUT_DIR / f"hf_comments_all{shard_suffix()}.jsonl"
    DEP_FILE = OUTPUT_DIR / f"hf_model_dependencies{shard_suffix()}.jsonl"
    STATE_FILE = OUTPUT_DIR / f"state_hf_subset{shard_suffix()}.json"

    subset = select_subset(total=args.total, top_n=args.top_n, stratified_n=args.stratified_n)
    # 按 model_id 稳定哈希分片，保证跨运行/跨平台一致
    if TOTAL_SHARDS > 1:
        def shard_of(model_id: str) -> int:
            return int(hashlib.md5(model_id.encode("utf-8")).hexdigest(), 16) % TOTAL_SHARDS
        subset = [m for m in subset if shard_of(m.get("id", "")) == SHARD_INDEX]
        print(f"[main] 分片 {SHARD_INDEX}/{TOTAL_SHARDS}，本 shard 子集大小: {len(subset)}")
    else:
        print(f"[main] 子集大小: {len(subset)}")

    if args.dry_run:
        for m in subset[:20]:
            print(f"  {m.get('id')}  downloads={m.get('downloads')}  "
                  f"pipeline={m.get('pipeline_tag')}  library={m.get('library_name')}")
        return

    state = load_state()
    completed = set(state.get("completed", []))
    todo = [m for m in subset if m.get("id") not in completed]
    print(f"[main] 已完成 {len(completed)}，待采集 {len(todo)}")

    commit_f = open(COMMIT_FILE, "a", encoding="utf-8")
    comment_f = open(COMMENT_FILE, "a", encoding="utf-8")

    session = requests.Session()
    session.headers.update(HEADERS)

    done_this_run = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_model = {
                executor.submit(fetch_model_subset, session, m["id"]): m
                for m in todo
            }
            for future in as_completed(future_to_model):
                model = future_to_model[future]
                model_id = model["id"]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "model_id": model_id,
                        "crawled_at": time.time(),
                        "status": "error",
                        "error": str(e),
                    }

                # commits
                commit_record = {
                    "model_id": model_id,
                    "crawled_at": result["crawled_at"],
                    "result": result["commits"],
                }
                commit_f.write(json.dumps(commit_record, ensure_ascii=False) + "\n")

                # discussions / comments
                comment_record = {
                    "model_id": model_id,
                    "crawled_at": result["crawled_at"],
                    "result": result["discussions"],
                }
                comment_f.write(json.dumps(comment_record, ensure_ascii=False) + "\n")

                # tree / dependencies
                write_dep_record(model_id, result["tree"])

                completed.add(model_id)
                done_this_run += 1

                if done_this_run % SAVE_EVERY == 0:
                    state["completed"] = sorted(completed)
                    state["count"] = len(completed)
                    save_state(state)
                    print(f"[progress] 本运行 +{done_this_run}，累计 {len(completed)}/{len(subset)}")

                time.sleep(REQUEST_DELAY / args.workers)
    except KeyboardInterrupt:
        print("\n[main] 用户中断")
    finally:
        state["completed"] = sorted(completed)
        state["count"] = len(completed)
        save_state(state)
        commit_f.close()
        comment_f.close()
        print(f"[main] 结束，本运行 +{done_this_run}，累计完成 {len(completed)}/{len(subset)}")


if __name__ == "__main__":
    main()
