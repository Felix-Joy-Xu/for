# -*- coding: utf-8 -*-
"""魔搭全量模型清单爬虫 - OpenAPI 前缀切片版。

背景：
- 旧清单 models_all.json（63,565 个）是按 500 个机构收集的，只覆盖约 28%。
- openapi/v1/models 无过滤查询有 page_number × page_size ≤ 3000 的深度上限。
- 但 search 参数按模型名前缀匹配（大小写不敏感），每个搜索词独立享有 3000 窗口。

策略：BFS 前缀切片。从 36 个单字符（a-z0-9）开始，结果数 > 3000 的词
递归追加一个字符细分，直到每个词的结果都能完整翻页取完。按 id 去重。

输出: modelscope_output/models_full.jsonl（逐页追加，含 search_term）
      modelscope_output/models_full.json（结束时按 id 去重合并）
状态: modelscope_output/state_ms_models_full.json（已完成的词，断点续爬）

环境变量 MS_FULL_MAX_TERMS=N 限制本轮处理的叶子词数量（调试用）。
"""
import json
import os
import sys
import time
import requests
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_JSONL = BASE_DIR / "modelscope_output" / "models_full.jsonl"
OUTPUT_JSON = BASE_DIR / "modelscope_output" / "models_full.json"
STATE_FILE = BASE_DIR / "modelscope_output" / "state_ms_models_full.json"

API = "https://modelscope.cn/openapi/v1/models"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
WINDOW = 3000          # page_number × page_size 上限
PAGE_SIZE = 50
DELAY = 0.15
ABORT_AFTER = 50
MAX_DEPTH = 6
MAX_TERMS = int(os.environ.get("MS_FULL_MAX_TERMS", "0") or 0)


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


def crawl_term(term, total, out_f):
    """翻页取完一个词的全部结果。返回 (新增写入数, ok)。"""
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    written = 0
    for p in range(1, pages + 1):
        d, ok = get_page(term, p, PAGE_SIZE)
        if not ok:
            return written, False
        if d.get("_quota"):
            print(f"  [{term}] p{p} 触发配额，中止该词", flush=True)
            return written, False
        items = d.get("models") or []
        for it in items:
            it["_search_term"] = term
            out_f.write(json.dumps(it, ensure_ascii=False) + "\n")
            written += 1
        out_f.flush()
        if len(items) < PAGE_SIZE:
            break
        time.sleep(DELAY)
    return written, True


def main():
    done_terms = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            done_terms = set(json.load(f))

    queue = [c for c in ALPHABET if c not in done_terms]
    print(f"待探测词 {len(queue)}，已完成词 {len(done_terms)}", flush=True)

    consecutive_errors = 0
    terms_done_this_run = 0
    models_written = 0

    with open(OUTPUT_JSONL, "a", encoding="utf-8") as out_f:
        while queue:
            term = queue.pop(0)
            total = total_of(term)
            if total is None:
                consecutive_errors += 1
                print(f"[{term}] 计数失败（连续失败 {consecutive_errors}）", flush=True)
                queue.append(term)  # 放回队尾稍后重试
                if consecutive_errors >= ABORT_AFTER:
                    print("连续失败过多，中止。", flush=True)
                    sys.exit(3)
                time.sleep(5)
                continue

            if total == 0:
                done_terms.add(term)
                continue

            if total > WINDOW:
                if len(term) >= MAX_DEPTH:
                    print(f"[{term}] total={total} 超过窗口但已达最大深度，尽力翻页", flush=True)
                else:
                    children = [term + c for c in ALPHABET if term + c not in done_terms]
                    queue = children + queue
                    print(f"[{term}] total={total} > {WINDOW}，细分为 {len(children)} 个子词", flush=True)
                    done_terms.add(term)
                    continue

            print(f"[{term}] total={total}，开始翻页", flush=True)
            written, ok = crawl_term(term, total, out_f)
            models_written += written
            if not ok:
                consecutive_errors += 1
                print(f"[{term}] 翻页中断（已写 {written}），稍后将重试该词", flush=True)
                queue.append(term)
                if consecutive_errors >= ABORT_AFTER:
                    print("连续失败过多，中止。", flush=True)
                    sys.exit(3)
                time.sleep(5)
                continue

            consecutive_errors = 0
            done_terms.add(term)
            terms_done_this_run += 1
            if terms_done_this_run % 20 == 0:
                with open(STATE_FILE, "w", encoding="utf-8") as sf:
                    json.dump(sorted(done_terms), sf)
                print(f"=== 进度: 本轮完成 {terms_done_this_run} 词，累计模型 {models_written}，队列剩 {len(queue)} ===", flush=True)
            if MAX_TERMS and terms_done_this_run >= MAX_TERMS:
                print(f"达到 MS_FULL_MAX_TERMS={MAX_TERMS}，提前结束（调试模式）", flush=True)
                break
            time.sleep(DELAY)

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
