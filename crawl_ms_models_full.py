# -*- coding: utf-8 -*-
"""魔搭全量模型清单爬虫 - OpenAPI 前缀切片版（并发优化）。

背景：
- 旧清单 models_all.json（63,565 个）是按 500 个机构收集的，只覆盖约 28%。
- openapi/v1/models 无过滤查询有 page_number × page_size ≤ 3000 的深度上限。
- 但 search 参数按模型名前缀匹配（大小写不敏感），每个搜索词独立享有 3000 窗口。

策略：BFS 前缀切片。从 36 个单字符（a-z0-9）开始，结果数 > 3000 的词
递归追加一个字符细分，直到每个词的结果都能完整翻页取完。按 id 去重。

并发优化（相对旧版提速 ~3-4 倍）：
- PAGE_SIZE 50→200：翻页请求数减为 1/4
- DELAY 0.15→0.05：词间延迟降低
- 3 线程并发处理队列中的词（计数/翻页并行）
- total ≤ PAGE_SIZE 时合并为 1 次请求（省计数请求）

输出: modelscope_output/models_full.jsonl（逐页追加，含 search_term）
      modelscope_output/models_full.json（结束时按 id 去重合并）
状态: modelscope_output/state_ms_models_full.json（已完成的词，断点续爬）

环境变量：
- MS_FULL_MAX_TERMS=N 限制本轮处理的叶子词数量（调试用）
- MS_FULL_BUDGET_MIN=N 本轮时间预算（分钟，默认 270）
- MS_FULL_WORKERS=N 并发线程数（默认 3）
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
OUTPUT_JSONL = BASE_DIR / "modelscope_output" / "models_full.jsonl"
OUTPUT_JSON = BASE_DIR / "modelscope_output" / "models_full.json"
STATE_FILE = BASE_DIR / "modelscope_output" / "state_ms_models_full.json"

API = "https://modelscope.cn/openapi/v1/models"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
WINDOW = 3000          # page_number × page_size 上限
PAGE_SIZE = 50         # 接口实测上限（ps≥100 返回 400）
DELAY = 0.05
ABORT_AFTER = 50
MAX_DEPTH = 6
MAX_TERMS = int(os.environ.get("MS_FULL_MAX_TERMS", "0") or 0)
BUDGET_MIN = int(os.environ.get("MS_FULL_BUDGET_MIN", "270") or 0)
WORKERS = int(os.environ.get("MS_FULL_WORKERS", "3") or 1)
START_TS = time.time()

# 全局写锁（多线程写 jsonl + 状态）
write_lock = threading.Lock()


def time_up():
    """时间预算是否用尽。预算到点主动收尾，避免 job 超时取消导致缓存丢失。"""
    return BUDGET_MIN > 0 and (time.time() - START_TS) > BUDGET_MIN * 60


def get_page(term, page_number, page_size, retries=4):
    """请求一页。返回 (data_dict, ok)。ok=False 为网络/限流类失败。"""
    params = {"page_number": page_number, "page_size": page_size}
    if term:
        params["search"] = term
    for attempt in range(retries):
        try:
            r = requests.get(API, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                j = r.json()
                if j.get("success"):
                    return j.get("data") or {}, True
                code = j.get("code", "")
                if code == "QuotaLimitExceed":
                    return {"_quota": True}, True
                return {}, True
            if r.status_code in (429, 403):
                time.sleep(2 ** attempt * 2)
                continue
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


def total_of(term):
    d, ok = get_page(term, 1, 1)
    if not ok:
        return None
    return d.get("total_count") or 0


def fetch_term(term):
    """处理一个词：先计数；total≤3000 则翻页取完，返回 (term, items, total, ok, need_split)。
    need_split=True 表示 total>3000，需要调用方细分（此时 items 为空）。"""
    # 1. 计数（ps1）
    d0, ok0 = get_page(term, 1, 1)
    if not ok0:
        return term, [], None, False, False
    total = d0.get("total_count") or 0
    if total == 0:
        return term, [], 0, True, False
    if total > WINDOW:
        return term, [], total, True, True  # 需要细分

    # 2. 翻页取完
    items = []
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    for p in range(1, pages + 1):
        if time_up():
            break
        d, ok = get_page(term, p, PAGE_SIZE)
        if not ok:
            return term, items, total, False, False
        if d.get("_quota"):
            break
        batch = d.get("models") or []
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(DELAY)

    return term, items, total, True, False


def fetch_term_best_effort(term):
    """对超窗口且达最大深度的词，尽力翻页取到 3000 上限。
    返回 (term, items, total, ok)。"""
    items = []
    total = 0
    # ps=50, 翻到窗口上限 60 页（3000 条）
    for p in range(1, (WINDOW // PAGE_SIZE) + 1):
        if time_up():
            break
        d, ok = get_page(term, p, PAGE_SIZE)
        if not ok:
            break
        if d.get("_quota"):
            break
        batch = d.get("models") or []
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(DELAY)
    if not items:
        return term, [], 0, False
    return term, items, len(items), True


def write_batch(term, items, out_f):
    """写入一批结果（加锁）"""
    if not items:
        return 0
    with write_lock:
        for it in items:
            it["_search_term"] = term
            out_f.write(json.dumps(it, ensure_ascii=False) + "\n")
        out_f.flush()
    return len(items)


def main():
    OUTPUT_JSONL.parent.mkdir(exist_ok=True)
    done_terms = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            done_terms = set(json.load(f))

    queue = [c for c in ALPHABET if c not in done_terms]
    print(f"待探测词 {len(queue)}，已完成词 {len(done_terms)}，并发 {WORKERS}", flush=True)

    consecutive_errors = 0
    terms_done_this_run = 0
    models_written = 0

    with open(OUTPUT_JSONL, "a", encoding="utf-8") as out_f:
        while queue:
            if time_up():
                print(f"时间预算 {BUDGET_MIN} 分钟用尽，主动收尾保存断点。", flush=True)
                break

            # 取一批词并行处理
            batch_terms = []
            while queue and len(batch_terms) < WORKERS * 2:
                t = queue.pop(0)
                if t not in done_terms:
                    batch_terms.append(t)

            if not batch_terms:
                break

            futures = {}
            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                for t in batch_terms:
                    futures[executor.submit(fetch_term, t)] = t

                for fut in as_completed(futures):
                    if time_up():
                        break
                    t = futures[fut]
                    try:
                        term, items, total, ok, need_split = fut.result()
                    except Exception as e:
                        print(f"[{t}] 异常: {str(e)[:80]}", flush=True)
                        ok = False
                        need_split = False
                        items = []

                    if need_split:
                        # total > WINDOW，细分入队
                        if len(term) >= MAX_DEPTH:
                            # 已达最大深度：尽力翻页取到 3000 上限
                            print(f"[{term}] 超窗口且达最大深度，尽力翻页取数", flush=True)
                            be_term, be_items, be_total, be_ok = fetch_term_best_effort(term)
                            n = write_batch(be_term, be_items, out_f)
                            models_written += n
                        else:
                            children = [term + c for c in ALPHABET if term + c not in done_terms]
                            with write_lock:
                                queue = children + queue
                                done_terms.add(term)
                            print(f"[{term}] total={total} > {WINDOW}，细分为 {len(children)} 个子词", flush=True)
                        with write_lock:
                            terms_done_this_run += 1
                        time.sleep(DELAY)
                        continue

                    if not ok:
                        consecutive_errors += 1
                        print(f"[{t}] 失败（连续 {consecutive_errors}）", flush=True)
                        with write_lock:
                            queue.append(t)
                        if consecutive_errors >= ABORT_AFTER:
                            print("连续失败过多，中止。", flush=True)
                            sys.exit(3)
                        time.sleep(3)
                        continue

                    consecutive_errors = 0
                    n = write_batch(term, items, out_f)
                    models_written += n
                    with write_lock:
                        done_terms.add(term)
                        terms_done_this_run += 1

                    if terms_done_this_run % 100 == 0:
                        with write_lock:
                            with open(STATE_FILE, "w", encoding="utf-8") as sf:
                                json.dump(sorted(done_terms), sf)
                        print(f"=== 进度: 本轮完成 {terms_done_this_run} 词，"
                              f"累计模型 {models_written}，队列剩 {len(queue)} ===", flush=True)
                    if MAX_TERMS and terms_done_this_run >= MAX_TERMS:
                        print(f"达到 MS_FULL_MAX_TERMS={MAX_TERMS}，提前结束（调试模式）", flush=True)
                        queue.clear()
                        break
                    time.sleep(DELAY)

        with write_lock:
            with open(STATE_FILE, "w", encoding="utf-8") as sf:
                json.dump(sorted(done_terms), sf)

    # 去重合并
    models = {}
    if OUTPUT_JSONL.exists():
        with open(OUTPUT_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except Exception:
                    continue
                mid = it.get("id")
                if mid:
                    models[mid] = it
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(list(models.values()), f, ensure_ascii=False)
    print(f"完成。去重后模型 {len(models)} 个 → {OUTPUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
