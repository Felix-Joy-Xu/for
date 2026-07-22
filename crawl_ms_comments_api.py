import os as _os
try:
    from _secrets import MODELSCOPE_TOKEN, MODELSCOPE_TOKENS
except ImportError:
    MODELSCOPE_TOKEN = _os.environ.get("MODELSCOPE_TOKEN", "")
    MODELSCOPE_TOKENS = [t for t in _os.environ.get("MODELSCOPE_TOKENS", "").split(",") if t]

"""
魔搭评论爬虫 - API 直采版
替代 crawl_ms_comments.py（Playwright 浏览器版，约 24 秒/个）。
直接请求 comments/summary 接口（约 0.5 秒/个），5 线程并发。
输出与状态文件格式与浏览器版完全一致，无缝断点续爬。
"""
import json
import sys
import time
import threading
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = Path(__file__).resolve().parent
_OUT = BASE_DIR / "modelscope_output"
# 优先使用全量清单（openapi 前缀切片产物），缺失时回退旧机构清单
MODELS_FILE = (_OUT / "models_full.json") if (_OUT / "models_full.json").exists() else (_OUT / "models_all.json")
OUTPUT_FILE = BASE_DIR / "modelscope_output" / "ms_comments_all.jsonl"
STATE_FILE = BASE_DIR / "modelscope_output" / "state_ms_comments.json"

TOKEN = MODELSCOPE_TOKEN
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Cookie": f"token={TOKEN}; ms_token={TOKEN}",
}

MAX_WORKERS = 5
SAVE_EVERY = 200           # 每完成 200 个写一次状态文件
ABORT_AFTER = 50           # 连续网络错误达到此数则中止（token 可能失效）

abort_flag = threading.Event()


def fetch_comments(model_id):
    """请求评论汇总接口。返回 (record, definitive)。
    definitive=True 表示得到确定答复（可标记完成）；False 表示网络/限流类错误，下次重试。
    """
    url = f"https://www.modelscope.cn/api/v1/models/{model_id}/comments/summary"
    record = {"model_id": model_id, "status": "success", "crawled_at": time.time()}

    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                record["api_intercepts"] = [{"url": url, "data": r.json()}]
                return record, True
            if r.status_code in (429, 403):
                time.sleep(2 ** attempt)  # 限流退避: 1s, 2s, 4s
                continue
            # 其他 4xx/5xx：重试一次后放弃（不标记完成）
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


def main():
    if not MODELS_FILE.exists():
        print(f"File not found: {MODELS_FILE}", flush=True)
        sys.exit(2)

    with open(MODELS_FILE, "r", encoding="utf-8") as f:
        models = json.load(f)

    models.sort(key=lambda x: x.get("Downloads") or x.get("downloads") or 0, reverse=True)

    completed = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            completed = set(json.load(f))

    # 兼容旧清单的 Id 字段与 openapi 全量清单的 id 字段
    todo = []
    for m in models:
        mid = m.get("Id") or m.get("id")
        if mid and mid not in completed:
            todo.append(mid)
    print(f"Total: {len(models)}, Completed: {len(completed)}, Remaining: {len(todo)}", flush=True)

    consecutive_errors = 0
    done_this_run = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as out_f:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_model = {executor.submit(fetch_comments, m_id): m_id for m_id in todo}
            for future in as_completed(future_to_model):
                if abort_flag.is_set():
                    break
                m_id = future_to_model[future]
                try:
                    record, definitive = future.result()
                except Exception as e:
                    record = {"model_id": m_id, "status": "error",
                              "error": str(e), "crawled_at": time.time()}
                    definitive = False

                out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_f.flush()

                if definitive:
                    completed.add(m_id)
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= ABORT_AFTER:
                        print("Too many consecutive errors, aborting (token may be invalid).", flush=True)
                        abort_flag.set()
                        break

                done_this_run += 1
                if done_this_run % SAVE_EVERY == 0:
                    with open(STATE_FILE, "w", encoding="utf-8") as sf:
                        json.dump(list(completed), sf)
                    print(f"Progress: +{done_this_run} this run, total done {len(completed)}", flush=True)

    with open(STATE_FILE, "w", encoding="utf-8") as sf:
        json.dump(list(completed), sf)
    print(f"Finished. +{done_this_run} this run, total done {len(completed)}", flush=True)
    if abort_flag.is_set():
        sys.exit(3)


if __name__ == "__main__":
    main()
