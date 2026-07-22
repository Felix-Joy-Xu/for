# -*- coding: utf-8 -*-
"""魔搭各板块评论汇总爬虫 - 多板块版。

覆盖模型区之外的 4 个板块（接口路径各不相同，均已实测可用）：
  skills   /api/v1/skills/{id}/comments/summary
  mcp      /api/v1/mcpServers/{id}/comments/summary
  datasets /api/v1/discussions/datasetsComment/{id}/comments/summary
  studios  /api/v1/studio/{owner}/{name}/comments/summary

每个板块独立输出与断点：
  modelscope_output/ms_comments_{kind}.jsonl
  modelscope_output/state_ms_comments_{kind}.json

5 线程并发，断点续爬。环境变量 MS_SECTIONS 可限定板块（逗号分隔）。
"""
import json
import os
import sys
import time
import threading
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = Path(__file__).resolve().parent
OUT = BASE_DIR / "modelscope_output"

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}


def studio_id(item):
    return f"{item.get('CreatedBy', '')}/{item.get('Name', '')}"


TARGETS = {
    "skills": {
        "api": "/api/v1/skills/{}/comments/summary",
        "source": "skills_all.json",
        "get_id": lambda it: it.get("id"),
    },
    "mcp": {
        "api": "/api/v1/mcpServers/{}/comments/summary",
        "source": "mcps_all.json",
        "get_id": lambda it: it.get("id"),
    },
    "datasets": {
        "api": "/api/v1/discussions/datasetsComment/{}/comments/summary",
        "source": "datasets_all.json",
        "get_id": lambda it: it.get("id"),
    },
    "studios": {
        "api": "/api/v1/studio/{}/comments/summary",
        "source": "studios_all.json",
        "get_id": studio_id,
    },
}

MAX_WORKERS = 5
SAVE_EVERY = 200
ABORT_AFTER = 50
LIMIT = int(os.environ.get("MS_MULTI_LIMIT", "0") or 0)

abort_flag = threading.Event()


def fetch_summary(kind, item_id, api_tpl):
    url = "https://www.modelscope.cn" + api_tpl.format(item_id)
    record = {"kind": kind, "target_id": item_id, "status": "success", "crawled_at": time.time()}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                record["api_intercepts"] = [{"url": url, "data": r.json()}]
                return record, True
            if r.status_code in (429, 403):
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                record["status"] = "error"
                record["error"] = "HTTP 404"
                return record, True  # 确定无此资源，标记完成
            if attempt < 1:
                time.sleep(1)
                continue
            record["status"] = "error"
            record["error"] = f"HTTP {r.status_code}"
            return record, False
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            record["status"] = "error"
            record["error"] = str(e)
            return record, False
    record["status"] = "error"
    record["error"] = "rate limited after retries"
    return record, False


def crawl_kind(kind, cfg):
    src = OUT / cfg["source"]
    if not src.exists():
        print(f"[{kind}] 源文件缺失: {src}", flush=True)
        return
    with open(src, "r", encoding="utf-8") as f:
        items = json.load(f)

    state_file = OUT / f"state_ms_comments_{kind}.json"
    out_file = OUT / f"ms_comments_{kind}.jsonl"

    completed = set()
    if state_file.exists():
        with open(state_file, "r", encoding="utf-8") as f:
            completed = set(json.load(f))

    todo = []
    for it in items:
        iid = cfg["get_id"](it)
        if iid and iid.strip("/") and iid not in completed:
            todo.append(iid)
    if LIMIT > 0:
        todo = todo[:LIMIT]
    print(f"[{kind}] 总数 {len(items)}，已完成 {len(completed)}，待采 {len(todo)}", flush=True)

    consecutive_errors = 0
    done_this_run = 0

    with open(out_file, "a", encoding="utf-8") as out_f:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_summary, kind, iid, cfg["api"]): iid for iid in todo}
            for future in as_completed(futures):
                if abort_flag.is_set():
                    break
                iid = futures[future]
                try:
                    record, definitive = future.result()
                except Exception as e:
                    record = {"kind": kind, "target_id": iid, "status": "error",
                              "error": str(e), "crawled_at": time.time()}
                    definitive = False

                out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_f.flush()

                if definitive:
                    completed.add(iid)
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= ABORT_AFTER:
                        print(f"[{kind}] 连续失败过多，中止。", flush=True)
                        abort_flag.set()
                        break

                done_this_run += 1
                if done_this_run % SAVE_EVERY == 0:
                    with open(state_file, "w", encoding="utf-8") as sf:
                        json.dump(sorted(completed), sf)
                    print(f"[{kind}] 进度 +{done_this_run}，累计 {len(completed)}", flush=True)

    with open(state_file, "w", encoding="utf-8") as sf:
        json.dump(sorted(completed), sf)
    print(f"[{kind}] 完成。本轮 +{done_this_run}，累计 {len(completed)}", flush=True)


def main():
    only = os.environ.get("MS_SECTIONS", "")
    kinds = [k.strip() for k in only.split(",") if k.strip()] if only else list(TARGETS.keys())
    for kind in kinds:
        if abort_flag.is_set():
            break
        crawl_kind(kind, TARGETS[kind])
    if abort_flag.is_set():
        sys.exit(3)


if __name__ == "__main__":
    main()
