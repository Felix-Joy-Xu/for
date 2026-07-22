# -*- coding: utf-8 -*-
"""魔搭评论正文爬虫 - 多板块 API 直采版。

在汇总数据基础上，抓取有社区互动的目标的完整内容：
- 4 类主帖：comment（评价）、issue（open/closed）、discussion、pr（open/closed）
- 每个主帖的全部回复
- 富文本 Content 提取为纯文本 content_text

板块配置（接口前缀均已实测）：
  models   /api/v1/models/{}                （源 ms_comments_all.jsonl）
  skills   /api/v1/skills/{}                （源 ms_comments_skills.jsonl）
  mcp      /api/v1/mcpServers/{}            （源 ms_comments_mcp.jsonl）
  datasets /api/v1/discussions/datasetsComment/{} （源 ms_comments_datasets.jsonl）
  studios  /api/v1/studio/{}                （源 ms_comments_studios.jsonl）

每个板块独立输出与断点。环境变量：
  MS_TEXT_SECTIONS=models,skills  限定板块（默认全部）
  MS_TEXT_LIMIT=N                 每板块本轮最多处理 N 个（调试用）
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

SECTIONS = {
    "models": {
        "api": "/api/v1/models/{}",
        "summary": "ms_comments_all.jsonl",
        "output": "ms_comment_texts.jsonl",
        "state": "state_ms_comment_texts.json",
        "id_field": "model_id",
    },
    "skills": {
        "api": "/api/v1/skills/{}",
        "summary": "ms_comments_skills.jsonl",
        "output": "ms_comment_texts_skills.jsonl",
        "state": "state_ms_comment_texts_skills.json",
        "id_field": "target_id",
    },
    "mcp": {
        "api": "/api/v1/mcpServers/{}",
        "summary": "ms_comments_mcp.jsonl",
        "output": "ms_comment_texts_mcp.jsonl",
        "state": "state_ms_comment_texts_mcp.json",
        "id_field": "target_id",
    },
    "datasets": {
        "api": "/api/v1/discussions/datasetsComment/{}",
        "summary": "ms_comments_datasets.jsonl",
        "output": "ms_comment_texts_datasets.jsonl",
        "state": "state_ms_comment_texts_datasets.json",
        "id_field": "target_id",
    },
    "studios": {
        "api": "/api/v1/studio/{}",
        "summary": "ms_comments_studios.jsonl",
        "output": "ms_comment_texts_studios.jsonl",
        "state": "state_ms_comment_texts_studios.json",
        "id_field": "target_id",
    },
}

COMBOS = [
    ("issue", "open"),
    ("issue", "closed"),
    ("pr", "open"),
    ("pr", "closed"),
    ("discussion", None),
    ("comment", None),
]

MAX_WORKERS = 5
PAGE_SIZE = 100
SAVE_EVERY = 50
ABORT_AFTER = 50
LIMIT = int(os.environ.get("MS_TEXT_LIMIT", "0") or 0)

abort_flag = threading.Event()


def extract_text(node):
    parts = []
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        if len(node) >= 3 and isinstance(node[1], dict) and node[1].get("data-type") == "leaf":
            if isinstance(node[2], str):
                return node[2]
        for child in node:
            t = extract_text(child)
            if t:
                parts.append(t)
    return "".join(parts)


def content_to_text(raw):
    if not raw:
        return ""
    try:
        tree = json.loads(raw)
        return extract_text(tree).strip()
    except Exception:
        return str(raw)[:500]


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json(), True
            if r.status_code in (429, 403):
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return {}, True
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
            return {}, False
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {}, False
    return {}, False


def fetch_threads(base_url, ctype, open_status):
    threads = []
    offset = 0
    while True:
        url = (f"{base_url}/comments/list"
               f"?Offset={offset}&PageSize={PAGE_SIZE}&PageNumber={offset // PAGE_SIZE + 1}&Type={ctype}")
        if open_status:
            url += f"&OpenStatus={open_status}"
        j, ok = get_json(url)
        if not ok:
            return threads, False
        data = j.get("Data") or {}
        batch = data.get("Comments") or []
        threads.extend(batch)
        total = data.get("TotalCount") or 0
        if len(batch) < PAGE_SIZE or len(threads) >= total:
            break
        offset += PAGE_SIZE
        time.sleep(0.15)
    return threads, True


def fetch_replies(base_url, comment_id):
    url = f"{base_url}/comments/{comment_id}?PageSize=1000&Offset=0"
    j, ok = get_json(url)
    if not ok:
        return [], False
    return (j.get("Data") or {}).get("Comments") or [], True


def slim_creator(c):
    c = c or {}
    return {"id": c.get("Id"), "name": c.get("Name"), "nickname": c.get("NickName")}


def fetch_target(kind, item_id, api_tpl):
    base_url = "https://www.modelscope.cn" + api_tpl.format(item_id)
    record = {"kind": kind, "target_id": item_id, "status": "success", "crawled_at": time.time()}
    all_threads = []
    seen_ids = set()
    net_fail = False

    for ctype, open_status in COMBOS:
        if abort_flag.is_set():
            break
        raw_threads, ok = fetch_threads(base_url, ctype, open_status)
        if not ok:
            net_fail = True
            continue
        for t in raw_threads:
            tid = t.get("Id")
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            replies = []
            if (t.get("TotalChildren") or 0) > 0:
                reps, rok = fetch_replies(base_url, tid)
                if rok:
                    replies = [{
                        "id": r.get("Id"),
                        "content_text": content_to_text(r.get("Content")),
                        "creator": slim_creator(r.get("Creator")),
                        "gmt_created": r.get("GmtCreated"),
                        "favorite_count": r.get("FavoriteCount"),
                    } for r in reps]
                time.sleep(0.15)
            all_threads.append({
                "id": tid,
                "type": t.get("Type") or ctype,
                "open_status": open_status,
                "is_open": t.get("IsOpen"),
                "title": t.get("Title") or "",
                "content_text": content_to_text(t.get("Content")),
                "score": t.get("Score"),
                "favorite_count": t.get("FavoriteCount"),
                "creator": slim_creator(t.get("Creator")),
                "gmt_created": t.get("GmtCreated"),
                "total_children": t.get("TotalChildren") or 0,
                "tags": t.get("Tags") or [],
                "replies": replies,
            })
        time.sleep(0.15)

    record["threads"] = all_threads
    record["thread_count"] = len(all_threads)
    record["reply_count"] = sum(len(t["replies"]) for t in all_threads)
    if net_fail and not all_threads:
        record["status"] = "error"
        record["error"] = "network failure on all combos"
        return record, False
    if net_fail:
        record["partial"] = True
    return record, True


def load_active(summary_path, id_field):
    active = {}
    with open(summary_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            iid = r.get(id_field)
            if not iid or iid in active:
                continue
            for it in r.get("api_intercepts") or []:
                d = (it.get("data") or {}).get("Data") or {}
                keys = ("Count", "DiscussionCount", "IssueOpenCount",
                        "IssueClosedCount", "PrOpenCount", "PrClosedCount", "TotalCount")
                if any((d.get(k) or 0) > 0 for k in keys):
                    active[iid] = True
                    break
    return sorted(active.keys())


def crawl_section(kind, cfg):
    summary_path = OUT / cfg["summary"]
    if not summary_path.exists():
        print(f"[{kind}] 汇总文件缺失: {summary_path}，跳过", flush=True)
        return
    active = load_active(summary_path, cfg["id_field"])

    state_file = OUT / cfg["state"]
    out_file = OUT / cfg["output"]
    completed = set()
    if state_file.exists():
        with open(state_file, "r", encoding="utf-8") as f:
            completed = set(json.load(f))

    todo = [i for i in active if i not in completed]
    if LIMIT > 0:
        todo = todo[:LIMIT]
    print(f"[{kind}] 活跃 {len(active)}，已完成 {len(completed)}，待采 {len(todo)}", flush=True)

    consecutive_errors = 0
    done_this_run = 0

    with open(out_file, "a", encoding="utf-8") as out_f:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_target, kind, iid, cfg["api"]): iid for iid in todo}
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
    only = os.environ.get("MS_TEXT_SECTIONS", "")
    # 默认不含 studios：其 comments/list 接口对匿名和登录态均 403（只能取汇总）
    kinds = ([k.strip() for k in only.split(",") if k.strip()] if only
             else ["models", "skills", "mcp", "datasets"])
    for kind in kinds:
        if abort_flag.is_set():
            break
        crawl_section(kind, SECTIONS[kind])
    if abort_flag.is_set():
        sys.exit(3)


if __name__ == "__main__":
    main()
